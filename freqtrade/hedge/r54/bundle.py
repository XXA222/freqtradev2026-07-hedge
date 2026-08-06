from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .canonical import atomic_write_json, sha256_file, sha256_json


def build_evidence_bundle(
    output_path: str | Path,
    files: Iterable[str | Path],
    *,
    account_id: str,
    bundle_type: str = "R5.4_OPERATIONAL_EVIDENCE",
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    resolved = tuple(Path(item).resolve(strict=True) for item in files)
    names = [item.name for item in resolved]
    if len(names) != len(set(names)):
        raise ValueError("bundle input filenames must be unique")
    manifest = {
        "schema_version": 1,
        "bundle_type": bundle_type,
        "account_id": account_id,
        "created_at": datetime.now(UTC).isoformat(),
        "files": [
            {"name": item.name, "size_bytes": item.stat().st_size, "sha256": sha256_file(item)}
            for item in sorted(resolved, key=lambda value: value.name)
        ],
    }
    manifest["canonical_sha256"] = sha256_json(manifest)
    temporary_manifest = output.with_suffix(output.suffix + ".manifest.tmp.json")
    atomic_write_json(temporary_manifest, manifest)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(temporary_manifest, "EVIDENCE-MANIFEST.json")
            for item in resolved:
                archive.write(item, f"evidence/{item.name}")
    finally:
        temporary_manifest.unlink(missing_ok=True)
    return output


def verify_evidence_bundle(path: str | Path) -> tuple[bool, tuple[str, ...]]:
    target = Path(path).resolve(strict=True)
    errors: list[str] = []
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        if "EVIDENCE-MANIFEST.json" not in names:
            return False, ("missing_manifest",)
        manifest = json.loads(archive.read("EVIDENCE-MANIFEST.json"))
        expected_canonical = str(manifest.pop("canonical_sha256", ""))
        if expected_canonical != sha256_json(manifest):
            errors.append("manifest_hash")
        for item in manifest.get("files", []):
            name = f"evidence/{item['name']}"
            if name not in names:
                errors.append(f"missing:{name}")
                continue
            data = archive.read(name)
            if len(data) != int(item["size_bytes"]):
                errors.append(f"size:{name}")
            import hashlib

            if hashlib.sha256(data).hexdigest() != str(item["sha256"]):
                errors.append(f"hash:{name}")
    return not errors, tuple(errors)
