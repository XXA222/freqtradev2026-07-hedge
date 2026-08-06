from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .canonical import sha256_file
from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class UpstreamFileExpectation:
    relative_path: str
    sha256: str
    required: bool = True


def verify_upstream_files(
    project_root: str | Path,
    expectations: tuple[UpstreamFileExpectation, ...],
) -> tuple[bool, Mapping[str, Any]]:
    root = Path(project_root).resolve(strict=True)
    mismatches: dict[str, str] = {}
    verified = 0
    for item in expectations:
        target = root / item.relative_path
        if not target.is_file():
            if item.required:
                mismatches[item.relative_path] = "MISSING"
            continue
        actual = sha256_file(target)
        if actual != item.sha256:
            mismatches[item.relative_path] = actual
        else:
            verified += 1
    return not mismatches, {"verified": verified, "mismatches": mismatches}


def build_upstream_evidence(
    *,
    project_root: str | Path,
    expectations: tuple[UpstreamFileExpectation, ...],
    account_id: str,
    upstream_version: str,
    upstream_commit: str,
) -> EvidenceEnvelope:
    passed, details = verify_upstream_files(project_root, expectations)
    return create_envelope(
        kind=ArtifactKind.UPSTREAM_REBASE,
        account_id=account_id,
        environment="SOURCE",
        producer="r54-upstream-verifier",
        payload={
            "upstream_version": upstream_version,
            "upstream_commit": upstream_commit,
            **details,
        },
        status=CheckStatus.PASS if passed else CheckStatus.FAIL,
        ttl_seconds=30 * 24 * 3600,
        write_surface="NONE",
    )
