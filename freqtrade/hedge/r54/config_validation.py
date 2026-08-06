from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


_VALID_TYPES = {"FULL_CONFIG", "OVERLAY", "DEPLOYMENT_MANIFEST", "DEPRECATED"}


def validate_config_manifest(path: str | Path, *, root: str | Path) -> tuple[bool, dict[str, Any]]:
    manifest_path = Path(path).resolve(strict=True)
    config_root = Path(root).resolve(strict=True)
    raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, Mapping):
        raise ValueError("config manifest must contain an object")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise ValueError("config manifest entries must be a list")
    errors: list[str] = []
    checked = 0
    names: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            errors.append(f"entry[{index}]:not_object")
            continue
        name = str(entry.get("path", "")).strip()
        kind = str(entry.get("type", "")).strip().upper()
        if not name or name in names:
            errors.append(f"entry[{index}]:invalid_or_duplicate_path")
            continue
        names.add(name)
        if kind not in _VALID_TYPES:
            errors.append(f"{name}:invalid_type")
            continue
        target = config_root / name
        if not target.is_file():
            errors.append(f"{name}:missing")
            continue
        if target.suffix.lower() == ".json":
            try:
                json.loads(target.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError:
                errors.append(f"{name}:invalid_json")
                continue
        checked += 1
    return not errors, {"checked": checked, "errors": errors}


def build_config_validation_evidence(
    *,
    manifest_path: str | Path,
    config_root: str | Path,
    account_id: str,
) -> EvidenceEnvelope:
    passed, details = validate_config_manifest(manifest_path, root=config_root)
    return create_envelope(
        kind=ArtifactKind.CONFIG_VALIDATION,
        account_id=account_id,
        environment="SOURCE",
        producer="r54-config-validator",
        payload=details,
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        ttl_seconds=30 * 24 * 3600,
        write_surface="NONE",
    )
