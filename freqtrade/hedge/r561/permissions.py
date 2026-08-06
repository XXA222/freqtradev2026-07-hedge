"""Writable-volume probe independent of chown support."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import secrets


@dataclass(frozen=True, slots=True)
class PermissionProbeReport:
    ready: bool
    root: str
    write_ok: bool
    fsync_ok: bool
    read_ok: bool
    delete_ok: bool
    reasons: tuple[str, ...]


def probe_directory(path: str | Path) -> PermissionProbeReport:
    root = Path(path)
    reasons: list[str] = []
    write_ok = fsync_ok = read_ok = delete_ok = False
    probe = root / f".hedge-write-probe-{secrets.token_hex(8)}"
    payload = secrets.token_bytes(64)
    try:
        root.mkdir(parents=True, exist_ok=True)
        with probe.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            write_ok = True
            os.fsync(handle.fileno())
            fsync_ok = True
        read_ok = probe.read_bytes() == payload
        if not read_ok:
            reasons.append("PERMISSION_PROBE_READ_MISMATCH")
    except Exception as exc:
        reasons.append(f"PERMISSION_PROBE_IO_FAILED:{type(exc).__name__}:{exc}")
    finally:
        try:
            if probe.exists():
                probe.unlink()
            delete_ok = not probe.exists()
        except Exception as exc:
            reasons.append(f"PERMISSION_PROBE_DELETE_FAILED:{type(exc).__name__}:{exc}")
    if not write_ok:
        reasons.append("PERMISSION_PROBE_WRITE_FAILED")
    if not fsync_ok:
        reasons.append("PERMISSION_PROBE_FSYNC_FAILED")
    if not read_ok:
        reasons.append("PERMISSION_PROBE_READ_FAILED")
    if not delete_ok:
        reasons.append("PERMISSION_PROBE_DELETE_FAILED")
    return PermissionProbeReport(
        ready=not reasons,
        root=str(root),
        write_ok=write_ok,
        fsync_ok=fsync_ok,
        read_ok=read_ok,
        delete_ok=delete_ok,
        reasons=tuple(dict.fromkeys(reasons)),
    )
