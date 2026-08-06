"""Redacted Docker runtime evidence bundle."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import zipfile
from typing import Any, Mapping

_SECRET_KEY = re.compile(r"(?i)(secret|password|token|api[_-]?key|private[_-]?key)")


def redact(value: Any, *, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "***REDACTED***"
    if is_dataclass(value):
        return redact(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def create_evidence_bundle(
    *,
    destination: str | Path,
    evidence: Mapping[str, Any],
    attachments: Mapping[str, str | Path] | None = None,
) -> Path:
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "mainnet_writes": "LOCKED",
        "evidence": redact(evidence),
    }
    entries: dict[str, bytes] = {
        "evidence.json": (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    }
    for logical_name, raw_path in (attachments or {}).items():
        path = Path(raw_path)
        if path.is_file():
            safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", logical_name)
            entries[f"attachments/{safe_name}"] = path.read_bytes()
    manifest = {
        name: sha256(content).hexdigest() for name, content in sorted(entries.items())
    }
    entries["manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)
    return target
