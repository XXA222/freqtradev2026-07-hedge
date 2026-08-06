import json
from decimal import Decimal

from freqtrade.hedge.r4.lifecycle import R4RuntimeLifecycle
from freqtrade.hedge.r4.models import ComponentHealth, ComponentState, RuntimePhase
from freqtrade.hedge.r4.observability import JsonlEventSink, MetricsRegistry


class Component:
    def __init__(self, name, *, fail_start=False, fail_stop=False, required=True):
        self.name = name
        self.required = required
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.state = ComponentState.UNKNOWN

    def start(self):
        if self.fail_start:
            raise RuntimeError("start failed")
        self.state = ComponentState.READY

    def stop(self):
        if self.fail_stop:
            raise RuntimeError("stop failed")
        self.state = ComponentState.STOPPED

    def health(self):
        return ComponentHealth(self.name, self.state, self.required)


def test_lifecycle_fails_closed_and_skips_downstream_components():
    lifecycle = R4RuntimeLifecycle(
        (Component("a", fail_start=True), Component("b"), Component("c", fail_start=True))
    )
    transitions = lifecycle.start()
    assert len(transitions) == 3
    assert lifecycle.phase is RuntimePhase.DEGRADED
    assert transitions[1].action == "skip"
    assert transitions[1].success is False


def test_lifecycle_stops_all_components_even_after_stop_failure():
    a = Component("a")
    b = Component("b", fail_stop=True)
    c = Component("c")
    lifecycle = R4RuntimeLifecycle((a, b, c))
    lifecycle.start()
    transitions = lifecycle.stop()
    assert len(transitions) == 3
    assert lifecycle.phase is RuntimePhase.FAILED
    assert sum(not item.success for item in transitions) == 1
    assert a.state is ComponentState.STOPPED


def test_metrics_and_events_are_atomic_and_redacted(tmp_path):
    metrics = MetricsRegistry()
    metrics.increment("cycles", 2)
    metrics.gauge("equity", Decimal("1000.25"))
    metrics.write_atomic(tmp_path / "metrics.json")
    assert json.loads((tmp_path / "metrics.json").read_text()) == {
        "cycles": "2", "equity": "1000.25"
    }
    sink = JsonlEventSink(tmp_path / "events.jsonl")
    sink.emit("auth", {"api_key": "abc", "nested": {"token": "xyz"}, "ok": True})
    record = json.loads((tmp_path / "events.jsonl").read_text())
    assert record["payload"]["api_key"] == "[REDACTED]"
    assert record["payload"]["nested"]["token"] == "[REDACTED]"
