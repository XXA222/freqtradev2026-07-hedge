from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .credentials import MAINNET_CREDENTIAL_MARKER


_MAX_REPORT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class R51Workspace:
    project_root: Path
    output_dir: Path
    r24_report: Path
    r4_report: Path
    credentials_path: Path
    config_path: Path
    stream_database: Path
    stream_observations: Path
    stream_report: Path
    r5_report: Path
    acceptance_report: Path
    run_id: str
    account_fingerprint: str
    r24_account_fingerprint: str
    removed_generated_credential_copy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "toolkit_version": "5.1.6",
            "project_root": str(self.project_root),
            "output_dir": str(self.output_dir),
            "r24_report": str(self.r24_report),
            "r4_report": str(self.r4_report),
            "credentials_path": str(self.credentials_path),
            "credentials_persisted_by_r51": False,
            "removed_generated_credential_copy": self.removed_generated_credential_copy,
            "config_path": str(self.config_path),
            "stream_database": str(self.stream_database),
            "stream_observations": str(self.stream_observations),
            "stream_report": str(self.stream_report),
            "r5_report": str(self.r5_report),
            "acceptance_report": str(self.acceptance_report),
            "run_id": self.run_id,
            "account_fingerprint": self.account_fingerprint,
            "r24_account_fingerprint": self.r24_account_fingerprint,
            "exchange_write": "LOCKED",
            "real_mainnet_order_requests_planned": 0,
        }


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        if path.stat().st_size > _MAX_REPORT_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _find_latest(root: Path, predicate: Callable[[Mapping[str, Any]], bool], name: str) -> Path:
    candidates: list[tuple[int, Path]] = []
    if root.exists():
        for path in root.rglob("*.json"):
            value = _read_json(path)
            if value is not None and predicate(value):
                candidates.append((path.stat().st_mtime_ns, path.resolve()))
    if not candidates:
        raise FileNotFoundError(f"no passing {name} report was found under {root}")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _is_r24(value: Mapping[str, Any]) -> bool:
    status = str(value.get("status", ""))
    security = value.get("security")
    gates = value.get("gate_domains")
    return (
        status in {"FULL_PASS", "PASS_WITH_DUAL_LEG_OBSERVATION_GAP"}
        and isinstance(security, Mapping)
        and security.get("exchange_writes") == "LOCKED"
        and isinstance(gates, Mapping)
        and all(
            gates.get(name) == "PASS"
            for name in (
                "clock_readiness",
                "authenticated_read_access",
                "account_and_hedge_semantics",
                "database_persistence",
                "history_completeness",
                "money_movement_safety",
            )
        )
        and isinstance(value.get("account_fingerprint"), str)
        and bool(str(value.get("account_fingerprint", "")).strip())
    )


def _is_r4(value: Mapping[str, Any]) -> bool:
    return (
        value.get("status") == "R4_FULL_COMPLETION_COMPLETE_MAINNET_LOCKED"
        and value.get("mainnet_live_exchange_write") == "LOCKED"
        and int(value.get("real_mainnet_order_requests_sent", 0)) == 0
    )


def _load_legacy_credentials(path: Path) -> tuple[str, str, Path]:
    resolved = path.expanduser().resolve(strict=True)
    lines = resolved.read_text(encoding="utf-8-sig").splitlines()
    values = [line.strip() for line in lines if line.strip()]
    if len(values) == 3 and values[2] == MAINNET_CREDENTIAL_MARKER:
        values = values[:2]
    if len(values) != 2:
        raise ValueError("legacy Binance credential file must contain key and secret on two lines")
    key, secret = values
    if len(key) < 16 or len(secret) < 16 or len(key) > 512 or len(secret) > 512:
        raise ValueError("legacy Binance credentials are invalid")
    return key, secret, resolved


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    with temporary.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, mode)
    except OSError:
        pass
    os.replace(temporary, path)


def _remove_previous_generated_copy(root: Path, key: str, secret: str) -> bool:
    """Remove only the exact credential copy generated by R5.1.0-R5.1.4.

    The source credential file is never removed. A differently shaped or differently
    valued file is left untouched, so operator-managed R5 credentials cannot be
    deleted accidentally.
    """
    generated = root / "user_data" / "r5" / "binance-mainnet-r5.txt"
    if not generated.exists() or not generated.is_file():
        return False
    try:
        values = [item.strip() for item in generated.read_text(encoding="utf-8-sig").splitlines() if item.strip()]
    except (OSError, UnicodeError):
        return False
    if values != [key, secret, MAINNET_CREDENTIAL_MARKER]:
        return False
    generated.unlink()
    return True


def prepare_readonly_workspace(
    *,
    project_root: str | Path,
    legacy_credentials: str | Path,
    output_dir: str | Path,
    require_ip_restriction: bool = True,
    require_futures_permission: bool = True,
) -> R51Workspace:
    root = Path(project_root).expanduser().resolve(strict=True)
    artifacts = root / "artifacts"
    output = Path(output_dir).expanduser().resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    r24 = _find_latest(artifacts, _is_r24, "R2.4.1")
    r4 = _find_latest(artifacts, _is_r4, "R4")
    r24_payload = _read_json(r24)
    if r24_payload is None:
        raise ValueError("R2.4.1 report could not be loaded")
    r24_fingerprint = str(r24_payload.get("account_fingerprint", "")).strip()
    if not r24_fingerprint:
        raise ValueError("R2.4.1 report does not contain account_fingerprint")

    key, secret, credential_path = _load_legacy_credentials(Path(legacy_credentials))
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    removed_copy = _remove_previous_generated_copy(root, key, secret)

    stream_report = output / "R5.1-FRESH-USER-STREAM-READINESS.json"
    config_path = output / "R5.1-MAINNET-READONLY-CONFIG.json"
    config = {
        "hedge_r5": {
            "credentials_path": str(credential_path),
            "journal_path": str(root / "user_data" / "r5" / "r5-mainnet-journal.sqlite"),
            "budget_path": str(root / "user_data" / "r5" / "r5-daily-budget.sqlite"),
            "user_stream_report": str(stream_report),
            "r24_report": str(r24),
            "r4_report": str(r4),
            "evidence_max_age_seconds": 900,
            # These are promotion policy requirements. MAINNET_READONLY records them
            # but does not confuse a disabled TRADE permission with failed read access.
            "require_ip_restriction": bool(require_ip_restriction),
            "require_futures_permission": bool(require_futures_permission),
            "require_account_can_trade": True,
            "require_withdrawals_disabled": True,
            "require_reading_permission": True,
            "require_zero_unmanaged_positions": True,
        }
    }
    _atomic_write(
        config_path,
        (json.dumps(config, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii"),
        mode=0o644,
    )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"r51-mainnet-readonly-{stamp}-{fingerprint[:8]}"
    return R51Workspace(
        project_root=root,
        output_dir=output,
        r24_report=r24,
        r4_report=r4,
        credentials_path=credential_path,
        config_path=config_path,
        stream_database=output / "r51-user-stream.sqlite",
        stream_observations=output / "r51-user-stream-observations.jsonl",
        stream_report=stream_report,
        r5_report=output / "R5.1-MAINNET-READONLY-STAGE.json",
        acceptance_report=output / "R5.1-MAINNET-READONLY-ACCEPTANCE.json",
        run_id=run_id,
        account_fingerprint=fingerprint,
        r24_account_fingerprint=r24_fingerprint,
        removed_generated_credential_copy=removed_copy,
    )
