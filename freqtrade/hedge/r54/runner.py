from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Mapping

from .canonical import atomic_write_json
from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: int = 900
    cwd: str | None = None
    environment: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("command name is required")
        if not self.argv:
            raise ValueError("command argv is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    exit_code: int
    started_at: datetime
    completed_at: datetime
    timed_out: bool
    launch_error: str | None
    stdout_path: str
    stderr_path: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.launch_error is None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": (self.completed_at - self.started_at).total_seconds(),
            "timed_out": self.timed_out,
            "launch_error": self.launch_error,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "status": "PASS" if self.passed else "FAIL",
        }


@dataclass(frozen=True, slots=True)
class ContinueAllSummary:
    results: tuple[CommandResult, ...]
    started_at: datetime
    completed_at: datetime

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.results)

    @property
    def failed_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.results if not item.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": "PASS" if self.passed else "FAIL",
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "duration_seconds": (self.completed_at - self.started_at).total_seconds(),
            "step_count": len(self.results),
            "failed_count": len(self.failed_names),
            "failed_names": list(self.failed_names),
            "results": [item.to_dict() for item in self.results],
        }


def run_continue_all(
    specs: Iterable[CommandSpec],
    *,
    artifact_dir: str | Path,
    summary_path: str | Path | None = None,
) -> ContinueAllSummary:
    output = Path(artifact_dir)
    output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    results: list[CommandResult] = []
    for index, spec in enumerate(tuple(specs), start=1):
        prefix = f"{index:02d}_{_safe_name(spec.name)}"
        stdout_path = output / f"{prefix}.stdout.log"
        stderr_path = output / f"{prefix}.stderr.log"
        step_started = datetime.now(UTC)
        exit_code = 127
        timed_out = False
        launch_error: str | None = None
        stdout_text = ""
        stderr_text = ""
        try:
            completed = subprocess.run(
                spec.argv,
                cwd=spec.cwd,
                env=None if spec.environment is None else dict(spec.environment),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=spec.timeout_seconds,
                check=False,
            )
            exit_code = int(completed.returncode)
            stdout_text = completed.stdout
            stderr_text = completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            timed_out = True
            stdout_text = _coerce_output(exc.stdout)
            stderr_text = _coerce_output(exc.stderr)
            stderr_text += f"\nTimed out after {spec.timeout_seconds} seconds.\n"
        except OSError as exc:
            launch_error = f"{type(exc).__name__}: {exc}"
            stderr_text = launch_error + "\n"
        stdout_path.write_text(stdout_text, encoding="utf-8")
        stderr_path.write_text(stderr_text, encoding="utf-8")
        results.append(
            CommandResult(
                name=spec.name,
                argv=spec.argv,
                exit_code=exit_code,
                started_at=step_started,
                completed_at=datetime.now(UTC),
                timed_out=timed_out,
                launch_error=launch_error,
                stdout_path=stdout_path.name,
                stderr_path=stderr_path.name,
            )
        )
    summary = ContinueAllSummary(
        results=tuple(results),
        started_at=started,
        completed_at=datetime.now(UTC),
    )
    destination = Path(summary_path) if summary_path is not None else output / "SUMMARY.json"
    atomic_write_json(destination, summary.to_dict())
    return summary


def build_release_diagnostics_evidence(
    summary: ContinueAllSummary,
    *,
    account_id: str,
) -> EvidenceEnvelope:
    return create_envelope(
        kind=ArtifactKind.RELEASE_DIAGNOSTICS,
        account_id=account_id,
        environment="LOCAL",
        producer="r54-continue-all",
        payload=summary.to_dict(),
        status=CheckStatus.PASS if summary.passed else CheckStatus.FAIL,
        ttl_seconds=30 * 24 * 3600,
        write_surface="NONE",
    )


def _safe_name(value: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)
    return normalized.strip("_") or "step"


def _coerce_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
