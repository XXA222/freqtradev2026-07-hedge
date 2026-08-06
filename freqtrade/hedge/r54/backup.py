from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .canonical import sha256_file
from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class BackupEntry:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    root_name: str
    entries: tuple[BackupEntry, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.entries)

    def to_payload(self) -> dict[str, Any]:
        return {
            "root_name": self.root_name,
            "total_bytes": self.total_bytes,
            "entries": [
                {
                    "relative_path": item.relative_path,
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in self.entries
            ],
        }


def create_backup_manifest(root: str | Path) -> BackupManifest:
    source = Path(root).resolve(strict=True)
    entries = tuple(
        BackupEntry(
            relative_path=item.relative_to(source).as_posix(),
            size_bytes=item.stat().st_size,
            sha256=sha256_file(item),
        )
        for item in sorted(source.rglob("*"))
        if item.is_file()
    )
    return BackupManifest(root_name=source.name, entries=entries)


def verify_backup_manifest(
    root: str | Path, manifest: BackupManifest
) -> tuple[bool, tuple[str, ...]]:
    source = Path(root).resolve(strict=True)
    errors: list[str] = []
    expected = {item.relative_path: item for item in manifest.entries}
    actual_paths = {
        item.relative_to(source).as_posix()
        for item in source.rglob("*")
        if item.is_file()
    }
    for missing in sorted(set(expected) - actual_paths):
        errors.append(f"missing:{missing}")
    for extra in sorted(actual_paths - set(expected)):
        errors.append(f"extra:{extra}")
    for relative, entry in expected.items():
        target = source / relative
        if target.is_file():
            if target.stat().st_size != entry.size_bytes:
                errors.append(f"size:{relative}")
            elif sha256_file(target) != entry.sha256:
                errors.append(f"hash:{relative}")
    return not errors, tuple(errors)


def run_backup_restore_drill(
    source: str | Path,
    *,
    account_id: str,
    producer: str = "r54-backup-restore",
) -> EvidenceEnvelope:
    source_path = Path(source).resolve(strict=True)
    manifest = create_backup_manifest(source_path)
    with tempfile.TemporaryDirectory(prefix="r54-backup-drill-") as temporary:
        restored = Path(temporary) / source_path.name
        shutil.copytree(source_path, restored)
        passed, errors = verify_backup_manifest(restored, manifest)
    return create_envelope(
        kind=ArtifactKind.BACKUP_RESTORE_DRILL,
        account_id=account_id,
        environment="LOCAL",
        producer=producer,
        payload={**manifest.to_payload(), "errors": list(errors)},
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        ttl_seconds=30 * 24 * 3600,
        write_surface="NONE",
    )
