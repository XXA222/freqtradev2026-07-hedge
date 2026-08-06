from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from .models import R4CycleReport, RuntimeReadiness
from .observability import MetricsRegistry, _atomic_write_text, redact

_PROMETHEUS_NAME = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]*$")


class RuntimeStatusPublisher:
    def __init__(self, path: Path, metrics: MetricsRegistry) -> None:
        self.path = path
        self.metrics = metrics

    def publish(
        self,
        *,
        readiness: RuntimeReadiness,
        last_cycle: R4CycleReport | None,
    ) -> None:
        payload = {
            "readiness": asdict(readiness),
            "last_cycle": None if last_cycle is None else asdict(last_cycle),
            "metrics": self.metrics.snapshot(),
        }
        _atomic_write_text(
            self.path,
            json.dumps(redact(payload), sort_keys=True, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def prometheus_text(self) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for name, value in self.metrics.snapshot().items():
            safe = "".join(ch if ch.isalnum() or ch in "_:" else "_" for ch in name)
            if not safe or not _PROMETHEUS_NAME.fullmatch(safe):
                raise ValueError(f"invalid Prometheus metric name derived from {name!r}")
            full_name = f"freqtrade_hedge_{safe}"
            if full_name in seen:
                raise ValueError(f"Prometheus metric name collision: {full_name}")
            seen.add(full_name)
            lines.append(f"{full_name} {value}")
        return "\n".join(lines) + ("\n" if lines else "")
