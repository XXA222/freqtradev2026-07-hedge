from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .common import (
    atomic_write_json,
    atomic_write_text,
    run_command,
    sanitize_remote_url,
    sha256_file,
)


EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "artifacts",
    "backtest_results",
    "build",
    "data",
    "dist",
    "freqtrade-hedge",
    "hyperopt_results",
    "logs",
    "models",
    "node_modules",
    "plot",
    "venv",
}

EXCLUDED_SUFFIXES = {
    ".7z",
    ".db",
    ".bundle",
    ".feather",
    ".gz",
    ".key",
    ".log",
    ".parquet",
    ".p12",
    ".pem",
    ".pickle",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".xz",
    ".zip",
}


def _is_excluded(relative: Path) -> bool:
    parts_lower = tuple(part.lower() for part in relative.parts)
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in parts_lower[:-1]):
        return True
    name = relative.name.lower()
    if name.startswith(".env"):
        return True
    if name.startswith(("config-private", "config_private", "secret", "credential")):
        return True
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if len(parts_lower) >= 2 and parts_lower[0] == "user_data" and relative.suffix.lower() in {
        ".json",
        ".json5",
        ".yaml",
        ".yml",
    }:
        return True
    return False


def iter_snapshot_files(project_root: Path) -> Iterator[Path]:
    for root, directories, filenames in os.walk(project_root):
        root_path = Path(root)
        relative_root = root_path.relative_to(project_root)
        directories[:] = sorted(
            directory
            for directory in directories
            if not _is_excluded(relative_root / directory / "placeholder")
        )
        for filename in sorted(filenames):
            path = root_path / filename
            relative = path.relative_to(project_root)
            if path.is_symlink() or _is_excluded(relative):
                continue
            yield path


def _git_metadata(project_root: Path) -> dict[str, Any]:
    inside = run_command(("git", "rev-parse", "--is-inside-work-tree"), cwd=project_root)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return {"available": False}

    def git(*args: str) -> str:
        result = run_command(("git", *args), cwd=project_root)
        return result.stdout.strip() if result.returncode == 0 else ""

    remote = sanitize_remote_url(git("remote", "get-url", "origin"))
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "available": True,
        "branch": git("branch", "--show-current"),
        "commit": git("rev-parse", "HEAD"),
        "describe": git("describe", "--always", "--dirty", "--tags"),
        "origin": remote,
        "status_clean_before_snapshot": not bool(status),
        "status_porcelain": status.splitlines(),
    }


def _create_tag(project_root: Path, tag: str, git_metadata: dict[str, Any]) -> dict[str, str]:
    if not tag:
        return {"status": "not_requested", "tag": ""}
    if not git_metadata.get("available"):
        return {"status": "skipped_no_git", "tag": tag}
    if not git_metadata.get("status_clean_before_snapshot"):
        return {"status": "skipped_dirty_worktree", "tag": tag}
    existing = run_command(("git", "rev-parse", "--verify", f"refs/tags/{tag}"), cwd=project_root)
    if existing.returncode == 0:
        return {"status": "already_exists", "tag": tag, "commit": existing.stdout.strip()}
    created = run_command(
        ("git", "tag", "-a", tag, "-m", "Freqtrade-Hedge R2.2 FULL_PASS frozen baseline"),
        cwd=project_root,
    )
    if created.returncode != 0:
        return {"status": "failed", "tag": tag, "error": created.stderr.strip()}
    return {"status": "created", "tag": tag, "commit": str(git_metadata.get("commit", ""))}


def _create_portable_git_bundle(
    archive_path: Path,
    baseline_dir: Path,
    *,
    label: str,
    tag: str,
) -> dict[str, Any]:
    git_version = run_command(("git", "--version"), cwd=baseline_dir)
    if git_version.returncode != 0:
        return {
            "status": "skipped_git_unavailable",
            "error": git_version.stderr.strip(),
        }

    bundle_path = baseline_dir / "freqtrade-hedge-source.git.bundle"
    with tempfile.TemporaryDirectory(prefix="freqtrade-hedge-baseline-") as temporary:
        repository = Path(temporary) / "source"
        repository.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(repository)

        commands: list[tuple[str, ...]] = [
            ("git", "init", "--quiet"),
            ("git", "config", "user.name", "Freqtrade-Hedge Baseline"),
            ("git", "config", "user.email", "baseline@local.invalid"),
            ("git", "add", "--all"),
            ("git", "commit", "--quiet", "-m", f"{label} frozen source baseline"),
        ]
        if tag:
            commands.append(
                (
                    "git",
                    "tag",
                    "-a",
                    tag,
                    "-m",
                    f"{label} frozen source baseline",
                )
            )
        commands.append(("git", "bundle", "create", str(bundle_path), "--all"))

        for command in commands:
            result = run_command(command, cwd=repository, timeout=300)
            if result.returncode != 0:
                return {
                    "status": "failed",
                    "command": " ".join(command[:2]),
                    "error": result.stderr.strip() or result.stdout.strip(),
                }

        commit = run_command(("git", "rev-parse", "HEAD"), cwd=repository)
        verified = run_command(
            ("git", "bundle", "verify", str(bundle_path)),
            cwd=repository,
            timeout=300,
        )
        if verified.returncode != 0:
            return {
                "status": "failed_verification",
                "error": verified.stderr.strip() or verified.stdout.strip(),
            }

    return {
        "status": "created",
        "bundle": bundle_path.name,
        "bundle_sha256": sha256_file(bundle_path),
        "commit": commit.stdout.strip(),
        "tag": tag,
        "source": "secret_safe_snapshot",
    }


def create_baseline(
    project_root: Path,
    *,
    output_root: Path | None = None,
    label: str = "R2.2_FULL_PASS",
    tag: str = "hedge-r2.2-full-pass",
    require_clean: bool = False,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    if not (project_root / "freqtrade").is_dir():
        raise ValueError(f"Not a Freqtrade source tree: {project_root}")
    if not (project_root / "pyproject.toml").is_file():
        raise ValueError(f"Missing pyproject.toml: {project_root}")

    git_metadata = _git_metadata(project_root)
    if require_clean and not git_metadata.get("status_clean_before_snapshot", False):
        raise RuntimeError("Git worktree is not clean; baseline was not created")

    tag_result = _create_tag(project_root, tag, git_metadata)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_root = (output_root or project_root / "artifacts" / "hedge_baselines").resolve()
    baseline_dir = output_root / f"{label}_{timestamp}"
    baseline_dir.mkdir(parents=True, exist_ok=False)

    files = list(iter_snapshot_files(project_root))
    archive_path = baseline_dir / "freqtrade-hedge-source.zip"
    manifest_entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=False,
    ) as archive:
        for source_path in files:
            relative = PurePosixPath(source_path.relative_to(project_root).as_posix())
            digest = sha256_file(source_path)
            size = source_path.stat().st_size
            archive.write(source_path, relative.as_posix())
            manifest_entries.append({"path": relative.as_posix(), "sha256": digest, "size": size})

    for entry in manifest_entries:
        current = sha256_file(project_root / entry["path"])
        if current != entry["sha256"]:
            raise RuntimeError(f"Source changed while snapshot was being created: {entry['path']}")

    manifest_text = "".join(
        f"{entry['sha256']}  {entry['path']}\n" for entry in manifest_entries
    )
    atomic_write_text(baseline_dir / "source-manifest.sha256", manifest_text)

    portable_git = _create_portable_git_bundle(
        archive_path,
        baseline_dir,
        label=label,
        tag=tag,
    )

    dependency_result = run_command(
        (sys.executable, "-m", "pip", "freeze", "--all"), cwd=project_root, timeout=180
    )
    dependency_text = dependency_result.stdout if dependency_result.returncode == 0 else dependency_result.stderr
    atomic_write_text(baseline_dir / "python-dependencies.txt", dependency_text)

    freqtrade_version = run_command(
        (sys.executable, "-m", "freqtrade", "--version"), cwd=project_root, timeout=60
    )
    archive_sha256 = sha256_file(archive_path)
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": "2.3.2",
        "label": label,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "project_root": str(project_root),
        "safety": {
            "exchange_writes_locked": True,
            "secrets_and_private_configs_excluded": True,
            "databases_logs_market_data_excluded": True,
        },
        "git": git_metadata,
        "git_tag": tag_result,
        "portable_git": portable_git,
        "runtime": {
            "platform": platform.platform(),
            "python": sys.version,
            "python_executable": sys.executable,
            "freqtrade_version_stdout": freqtrade_version.stdout.strip(),
            "freqtrade_version_stderr": freqtrade_version.stderr.strip(),
            "freqtrade_version_returncode": freqtrade_version.returncode,
        },
        "snapshot": {
            "file_count": len(manifest_entries),
            "uncompressed_size_bytes": sum(entry["size"] for entry in manifest_entries),
            "archive": archive_path.name,
            "archive_size_bytes": archive_path.stat().st_size,
            "archive_sha256": archive_sha256,
            "manifest": "source-manifest.sha256",
        },
    }
    atomic_write_json(baseline_dir / "baseline.json", metadata)

    verification = {
        "baseline_json_sha256": sha256_file(baseline_dir / "baseline.json"),
        "manifest_sha256": sha256_file(baseline_dir / "source-manifest.sha256"),
        "archive_sha256": archive_sha256,
    }
    if portable_git.get("status") == "created":
        verification["portable_git_bundle_sha256"] = portable_git["bundle_sha256"]
    atomic_write_json(baseline_dir / "verification.json", verification)
    metadata["baseline_directory"] = str(baseline_dir)
    metadata["verification"] = verification
    return metadata
