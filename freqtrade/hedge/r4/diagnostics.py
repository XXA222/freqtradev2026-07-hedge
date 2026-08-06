from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Sequence

from .models import DiagnosticResult, DiagnosticSummary
from .observability import _atomic_write_text, redact


class DiagnosticRunner:
    """Run every diagnostic section even when earlier sections fail."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._results: list[DiagnosticResult] = []

    @staticmethod
    def _safe_name(name: str) -> str:
        normalized = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name).strip())
        if not normalized or normalized in {".", ".."}:
            raise ValueError("diagnostic name is invalid")
        return normalized[:160]

    def run_callable(self, name: str, callback: Callable[[], object]) -> DiagnosticResult:
        safe_name = self._safe_name(name)
        started = time.monotonic()
        try:
            value = callback()
            result = DiagnosticResult(safe_name, "PASS", time.monotonic() - started, 0, str(redact(value)))
        except Exception as exc:
            result = DiagnosticResult(
                safe_name,
                "FAIL",
                time.monotonic() - started,
                1,
                f"{type(exc).__name__}: {redact(str(exc))}",
            )
        self._results.append(result)
        return result

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[object]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    def run_command(
        self,
        name: str,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int = 1800,
    ) -> DiagnosticResult:
        safe_name = self._safe_name(name)
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("diagnostic command must contain non-empty strings")
        if timeout_seconds <= 0:
            raise ValueError("diagnostic timeout must be positive")
        stdout = self.output_dir / f"{safe_name}.stdout.txt"
        stderr = self.output_dir / f"{safe_name}.stderr.txt"
        started = time.monotonic()
        exit_code = 1
        status = "ERROR"
        summary = ""
        process: subprocess.Popen[object] | None = None
        try:
            creationflags = 0
            kwargs: dict[str, object] = {}
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            with stdout.open("w", encoding="utf-8", errors="replace", newline="\n") as out_handle, stderr.open(
                "w", encoding="utf-8", errors="replace", newline="\n"
            ) as err_handle:
                process = subprocess.Popen(
                    list(command),
                    cwd=cwd,
                    text=True,
                    stdout=out_handle,
                    stderr=err_handle,
                    shell=False,
                    creationflags=creationflags,
                    **kwargs,
                )
                try:
                    exit_code = process.wait(timeout=timeout_seconds)
                    status = "PASS" if exit_code == 0 else "FAIL"
                    summary = f"exit_code={exit_code}"
                except subprocess.TimeoutExpired:
                    self._terminate_process_tree(process)
                    status = "TIMEOUT"
                    exit_code = 124
                    summary = f"timeout after {timeout_seconds}s"
        except Exception as exc:
            if process is not None:
                self._terminate_process_tree(process)
            status = "ERROR"
            summary = f"{type(exc).__name__}: {redact(str(exc))}"
            stderr.write_text(summary + "\n", encoding="utf-8")
            stdout.touch(exist_ok=True)
        result = DiagnosticResult(
            safe_name,
            status,
            time.monotonic() - started,
            exit_code,
            summary,
            str(stdout),
            str(stderr),
            {"command": list(command), "python": sys.executable},
        )
        self._results.append(result)
        return result

    def finish(self, started_at: datetime) -> DiagnosticSummary:
        summary = DiagnosticSummary(
            started_at=started_at,
            completed_at=datetime.now(UTC),
            results=tuple(self._results),
            output_dir=self.output_dir,
        )
        payload = {
            "started_at": summary.started_at.isoformat(),
            "completed_at": summary.completed_at.isoformat(),
            "passed": summary.passed,
            "failure_count": len(summary.failed),
            "results": [redact(asdict(item)) for item in summary.results],
        }
        _atomic_write_text(
            self.output_dir / "R4-DIAGNOSTIC-SUMMARY.json",
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True),
            encoding="utf-8",
        )
        lines = [
            f"R4 diagnostic passed: {summary.passed}",
            f"Failures: {len(summary.failed)}",
            "",
        ]
        for item in summary.results:
            lines.append(f"[{item.status}] {item.name}: {item.summary}")
        _atomic_write_text(
            self.output_dir / "R4-DIAGNOSTIC-SUMMARY.txt",
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        return summary
