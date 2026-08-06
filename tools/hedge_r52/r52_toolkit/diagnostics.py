from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


TOOLKIT_VERSION = "5.2.3"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    temporary.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _toolkit_pythonpath_entries(root: Path) -> tuple[str, ...]:
    """Return deterministic import roots for all versioned Hedge toolkits.

    Several R3 compatibility tests intentionally import packages such as
    ``r30_toolkit`` and ``r38_toolkit`` as top-level modules.  Adding only the
    project root and ``tools`` directory is insufficient because those packages
    live one level deeper under ``tools/hedge_rXX``.
    """
    tools_root = root / "tools"
    entries: list[str] = [str(root), str(tools_root)]
    if tools_root.is_dir():
        for candidate in sorted(tools_root.glob("hedge_r*"), key=lambda item: item.name):
            if not candidate.is_dir():
                continue
            has_toolkit = any(
                child.is_dir() and child.name.endswith("_toolkit")
                for child in candidate.iterdir()
            )
            if has_toolkit:
                entries.append(str(candidate))
    # Preserve order while removing duplicates.
    return tuple(dict.fromkeys(entries))


def _run_section(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    output_dir: Path,
    timeout_seconds: int,
    env: Mapping[str, str],
) -> dict[str, Any]:
    stdout_path = output_dir / f"{name}.stdout.txt"
    stderr_path = output_dir / f"{name}.stderr.txt"
    started = time.monotonic()
    exit_code = 125
    timed_out = False
    launch_error: str | None = None
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env),
                stdout=stdout_file,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
                    else 0
                ),
            )
            try:
                exit_code = int(process.wait(timeout=timeout_seconds))
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                exit_code = 124
    except Exception as exc:  # keep later sections running
        launch_error = f"{type(exc).__name__}:{exc}"
        stderr_path.write_text(launch_error + "\n", encoding="utf-8")
    duration = round(time.monotonic() - started, 3)
    return {
        "name": name,
        "command": command,
        "exit_code": exit_code,
        "passed": exit_code == 0 and not timed_out and launch_error is None,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "duration_seconds": duration,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }


def _security_scan(project_root: Path, output_dir: Path) -> dict[str, Any]:
    targets = [
        project_root / "freqtrade/hedge/r5",
        project_root / "tools/hedge_r52",
        project_root / "scripts/Run-Freqtrade-Hedge-R5.2-MainnetShadow.ps1",
    ]
    findings: list[dict[str, Any]] = []
    forbidden = (
        "api_secret =",
        "api_key =",
        "BEGIN PRIVATE KEY",
        "OrderType.MARKET",
    )
    scanned = 0
    for target in targets:
        files: Iterable[Path]
        if target.is_dir():
            files = target.rglob("*")
        else:
            files = (target,)
        for path in files:
            if (
                not path.is_file()
                or path.suffix.lower() not in {".py", ".ps1", ".json", ".md"}
                or path.name in {"diagnostics.py", "offline_acceptance.py"}
            ):
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                findings.append({"path": str(path), "reason": f"READ_ERROR:{exc}"})
                continue
            for token in forbidden:
                if token in text:
                    findings.append({"path": str(path), "reason": f"FORBIDDEN_TOKEN:{token}"})
    shadow_files = [
        project_root / "freqtrade/hedge/r5/strategy_shadow.py",
        project_root / "tools/hedge_r52/r52_toolkit/__main__.py",
        project_root / "scripts/Run-Freqtrade-Hedge-R5.2-MainnetShadow.ps1",
    ]
    order_endpoint_tokens = ("/fapi/v1/order", "/fapi/v1/order/test", "/fapi/v1/batchOrders")
    for path in shadow_files:
        if not path.is_file():
            findings.append({"path": str(path), "reason": "MISSING_SHADOW_FILE"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in order_endpoint_tokens:
            if token in text:
                findings.append({"path": str(path), "reason": f"SHADOW_ORDER_ENDPOINT:{token}"})
    payload = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "status": "PASS" if not findings else "FAIL",
        "scanned_files": scanned,
        "finding_count": len(findings),
        "findings": findings,
        "installer_exchange_access": "NONE",
        "real_mainnet_order_requests_sent": 0,
    }
    _atomic_json(output_dir / "10_security_scan.json", payload)
    return {
        "name": "10_security_scan",
        "command": ["internal-security-scan"],
        "exit_code": 0 if not findings else 2,
        "passed": not findings,
        "timed_out": False,
        "launch_error": None,
        "duration_seconds": 0,
        "stdout": str(output_dir / "10_security_scan.json"),
        "stderr": "",
    }


def _zip_directory(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    temporary.replace(destination)


def run(args: argparse.Namespace) -> int:
    root = Path(args.project_root).expanduser().resolve(strict=True)
    python = Path(args.python).expanduser().resolve(strict=True)
    output = Path(args.output_dir).expanduser().resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "")
    toolkit_roots = _toolkit_pythonpath_entries(root)
    pythonpath_entries = [*toolkit_roots]
    if current_pythonpath:
        pythonpath_entries.extend(
            item for item in current_pythonpath.split(os.pathsep) if item
        )
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath_entries))
    env["PYTHONUTF8"] = "1"
    py = str(python)
    sections: list[dict[str, Any]] = []
    specs = [
        ("01_pip_check", [py, "-m", "pip", "check"], 300),
        (
            "02_compileall",
            [
                py,
                "-m",
                "compileall",
                "-q",
                "freqtrade/hedge/r5",
                "freqtrade/hedge/r4",
                "freqtrade/hedge/planning",
                "tools/hedge_r52",
            ],
            300,
        ),
        ("03_r52_tests", [py, "-m", "pytest", "-q", "tools/hedge_r52/tests"], 900),
        ("04_r51_tests", [py, "-m", "pytest", "-q", "tools/hedge_r51/tests"], 900),
        ("05_r50_tests", [py, "-m", "pytest", "-q", "tools/hedge_r50/tests"], 900),
        ("06_r40_tests", [py, "-m", "pytest", "-q", "tools/hedge_r40/tests"], 900),
        (
            "07_execution_compatibility",
            [
                py,
                "-m",
                "pytest",
                "-q",
                "tools/hedge_r30/tests",
                "tools/hedge_r31/tests",
                "tools/hedge_r32/tests",
                "tools/hedge_r33/tests",
                "tools/hedge_r34/tests",
                "tools/hedge_r35/tests",
                "tools/hedge_r36/tests",
                "tools/hedge_r37/tests",
                "tools/hedge_r38/tests",
            ],
            1200,
        ),
        ("08_full_hedge_regression", [py, "-m", "pytest", "-q", "tests/hedge"], 1800),
        (
            "09_r52_offline_acceptance",
            [
                py,
                "-m",
                "tools.hedge_r52.r52_toolkit.offline_acceptance",
                "--project-root",
                str(root),
                "--output",
                str(output / "R5.2-PRODUCTION-STRATEGY-CORE-READINESS.json"),
            ],
            600,
        ),
    ]
    for name, command, timeout in specs:
        sections.append(
            _run_section(
                name=name,
                command=command,
                cwd=root,
                output_dir=output,
                timeout_seconds=timeout,
                env=env,
            )
        )
    sections.append(_security_scan(root, output))
    failed = [section["name"] for section in sections if not section["passed"]]
    summary: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "R5_2_FULL_DIAGNOSTICS_PASS" if not failed else "R5_2_FULL_DIAGNOSTICS_FAIL",
        "all_sections_attempted": len(sections) == 10,
        "failed_sections": failed,
        "sections": sections,
        "installer_exchange_access": "NONE",
        "real_mainnet_order_requests_sent_by_installer": 0,
        "mainnet_live_exchange_write": "LOCKED",
        "pythonpath_toolkit_roots": list(toolkit_roots),
    }
    install_manifest = os.environ.get("R52_INSTALL_MANIFEST")
    if install_manifest:
        summary["install_manifest"] = install_manifest
    canonical = json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    summary["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    _atomic_json(output / "R5.2-DIAGNOSTIC-SUMMARY.json", summary)
    lines = [
        f"status={summary['status']}",
        f"all_sections_attempted={summary['all_sections_attempted']}",
        f"failed_sections={','.join(failed) if failed else 'NONE'}",
        "installer_exchange_access=NONE",
        "real_mainnet_order_requests_sent_by_installer=0",
        "",
    ]
    for section in sections:
        lines.append(
            f"{section['name']}: {'PASS' if section['passed'] else 'FAIL'} "
            f"exit={section['exit_code']} timeout={section['timed_out']} "
            f"duration={section['duration_seconds']}s"
        )
    _atomic_text(output / "R5.2-DIAGNOSTIC-SUMMARY.txt", "\n".join(lines) + "\n")
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    stamp = output.name
    upload = downloads / f"Freqtrade_Hedge_R5.2_InstallDiagnosticUpload_{stamp}.zip"
    _zip_directory(output, upload)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=True))
    print(f"Diagnostic upload ZIP: {upload}")
    return 0 if not failed else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r52-diagnostics")
    value.add_argument("--project-root", required=True)
    value.add_argument("--python", required=True)
    value.add_argument("--output-dir", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
