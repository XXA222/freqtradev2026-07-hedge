from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .strategy_runtime import (
    R5ProductionStrategyRuntime,
    R5StrategyFacts,
    R5StrategySignals,
)


class R5FactsProvider(Protocol):
    def __call__(self, cycle_index: int) -> R5StrategyFacts: ...


class R5SignalsProvider(Protocol):
    def __call__(self, cycle_index: int, facts: R5StrategyFacts) -> R5StrategySignals: ...


@dataclass(frozen=True, slots=True)
class R5StrategySessionConfig:
    session_id: str
    max_cycles: int = 60
    max_runtime_seconds: int = 3600
    cycle_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        text = str(self.session_id).strip()
        if not text or len(text) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise ValueError("R5.2 session id is invalid")
        if isinstance(self.max_cycles, bool) or not 1 <= self.max_cycles <= 10000:
            raise ValueError("R5.2 session max_cycles must be in [1, 10000]")
        if isinstance(self.max_runtime_seconds, bool) or not 1 <= self.max_runtime_seconds <= 86400:
            raise ValueError("R5.2 session max_runtime_seconds must be in [1, 86400]")
        interval = float(self.cycle_interval_seconds)
        if not 0 <= interval <= 300:
            raise ValueError("R5.2 session interval must be in [0, 300] seconds")
        object.__setattr__(self, "session_id", text)
        object.__setattr__(self, "cycle_interval_seconds", interval)


class R5StrategySession:
    """Bounded foreground strategy loop with durable per-cycle recovery semantics."""

    def __init__(
        self,
        *,
        runtime: R5ProductionStrategyRuntime,
        facts_provider: R5FactsProvider,
        signals_provider: R5SignalsProvider,
        config: R5StrategySessionConfig,
        output: str | Path,
        kill_switch: Callable[[], str | None] | None = None,
    ) -> None:
        profile_seconds = runtime.profile.max_runtime_minutes * 60
        if config.max_runtime_seconds > profile_seconds:
            raise ValueError("R5.2 session runtime exceeds the stage profile")
        self.runtime = runtime
        self.facts_provider = facts_provider
        self.signals_provider = signals_provider
        self.config = config
        self.output = Path(output).expanduser().resolve(strict=False)
        self.kill_switch = kill_switch

    def run(self) -> Mapping[str, Any]:
        started_at = datetime.now(UTC)
        started_mono = time.monotonic()
        cycle_reports: list[Mapping[str, Any]] = []
        status = "COMPLETED"
        stop_reason = "MAX_CYCLES"
        error: str | None = None

        for index in range(self.config.max_cycles):
            current_cycle_id: str | None = None
            elapsed = time.monotonic() - started_mono
            if elapsed >= self.config.max_runtime_seconds:
                stop_reason = "MAX_RUNTIME"
                break
            kill_reason = self._kill_reason()
            if kill_reason is not None:
                status = "KILLED"
                stop_reason = kill_reason
                break
            try:
                facts = self.facts_provider(index)
                signals = self.signals_provider(index, facts)
                current_cycle_id = self._cycle_id(index=index, facts=facts, signals=signals)
                result = self.runtime.run_cycle(
                    cycle_id=current_cycle_id,
                    facts=facts,
                    signals=signals,
                )
                cycle_reports.append(result.report)
            except Exception as exc:
                status = "FAILED"
                stop_reason = "CYCLE_FAILURE"
                error = f"{type(exc).__name__}:{exc}"
                cycle_row = (
                    self.runtime.store.cycle(current_cycle_id)
                    if current_cycle_id is not None
                    else None
                )
                if cycle_row is not None and str(cycle_row.get("status")) == "RECOVERY_REQUIRED":
                    status = "RECOVERY_REQUIRED"
                break
            if index + 1 < self.config.max_cycles and self.config.cycle_interval_seconds > 0:
                remaining = self.config.max_runtime_seconds - (time.monotonic() - started_mono)
                if remaining <= 0:
                    stop_reason = "MAX_RUNTIME"
                    break
                time.sleep(min(self.config.cycle_interval_seconds, remaining))
        else:
            stop_reason = "MAX_CYCLES"

        completed_at = datetime.now(UTC)
        writes = sum(int(item.get("writes_attempted", 0)) for item in cycle_reports)
        mainnet_writes = sum(int(item.get("mainnet_order_requests_sent", 0)) for item in cycle_reports)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "toolkit_version": "5.2.3",
            "session_id": self.config.session_id,
            "status": status,
            "stop_reason": stop_reason,
            "error": error,
            "mode": self.runtime.mode.value,
            "account_id": self.runtime.account_id,
            "symbol": self.runtime.symbol,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "duration_seconds": round((completed_at - started_at).total_seconds(), 6),
            "cycles_completed": len(cycle_reports),
            "writes_attempted": writes,
            "mainnet_order_requests_sent": mainnet_writes,
            "exchange_write": (
                "ENABLED_LIMITED" if self.runtime.mode.allows_exchange_write else "LOCKED"
            ),
            "cycles": cycle_reports,
            "database_integrity": self.runtime.store.integrity(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
        self._write_report(payload)
        return payload

    def _kill_reason(self) -> str | None:
        if self.kill_switch is None:
            return None
        value = self.kill_switch()
        if value is None:
            return None
        text = str(value).strip()
        return text[:256] if text else "KILL_SWITCH"

    def _cycle_id(
        self,
        *,
        index: int,
        facts: R5StrategyFacts,
        signals: R5StrategySignals,
    ) -> str:
        signal_payload = {
            "long": str(signals.long_signal),
            "short": str(signals.short_signal),
            "target_net": None if signals.target_net_quantity is None else str(signals.target_net_quantity),
            "volatility": str(signals.realized_volatility),
            "funding": str(signals.funding_rate),
        }
        digest = hashlib.sha256(
            json.dumps(signal_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        ).hexdigest()[:12]
        observed = facts.observed_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{self.config.session_id}:{index:06d}:{observed}:{digest}"

    def _write_report(self, payload: Mapping[str, Any]) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.output.with_suffix(self.output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
            encoding="ascii",
        )
        temporary.replace(self.output)
