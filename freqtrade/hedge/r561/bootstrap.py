"""Atomic, fail-closed Docker Dry-run configuration bootstrap."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
from typing import Any

from .config import ConfigGateReport, validate_docker_dryrun_config


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    ready: bool
    created: bool
    updated_placeholders: bool
    destination: str
    mode: str
    config_report: ConfigGateReport
    reasons: tuple[str, ...]


def _token(byte_count: int) -> str:
    return secrets.token_urlsafe(byte_count)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def bootstrap_docker_config(
    *,
    template_path: str | Path,
    destination: str | Path,
) -> BootstrapReport:
    template = Path(template_path)
    target = Path(destination)
    reasons: list[str] = []
    if not template.is_file():
        raise FileNotFoundError(f"Docker config template is missing: {template}")
    created = not target.exists()
    source = template if created else target
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"Unable to parse Docker config {source}: {type(exc).__name__}: {exc}") from exc
    api = payload.setdefault("api_server", {})
    if not isinstance(api, dict):
        raise ValueError("api_server must be an object")
    replacements = {
        "password": ("__GENERATE_PASSWORD__", _token(24)),
        "jwt_secret_key": ("__GENERATE_JWT_SECRET_32_CHARS__", _token(48)),
        "ws_token": ("__GENERATE_WS_TOKEN__", _token(32)),
    }
    changed = False
    for key, (placeholder, replacement) in replacements.items():
        current = str(api.get(key, ""))
        if current in {"", placeholder}:
            api[key] = replacement
            changed = True
    report = validate_docker_dryrun_config(payload, run_upstream_schema=False)
    if not report.ready:
        reasons.extend(report.errors)
        return BootstrapReport(False, created, changed, str(target), "docker_loopback_publish", report, tuple(reasons))
    if created or changed:
        _atomic_json(target, payload)
    return BootstrapReport(True, created, changed, str(target), "docker_loopback_publish", report, ())
