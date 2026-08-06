from pathlib import Path

import pytest

from freqtrade.hedge.r4.config import runtime_config_from_mapping
from freqtrade.hedge.r4.models import ComponentHealth, ComponentState, R4Mode, RuntimePhase
from freqtrade.hedge.r4.readiness import ReadinessAggregator


def test_config_accepts_locked_mainnet(tmp_path):
    config = runtime_config_from_mapping(
        {"hedge_r4": {"mode": "PRODUCTION_LOCKED", "account_id": "binance-usdm:x"}},
        project_root=tmp_path,
    )
    assert config.mode is R4Mode.PRODUCTION_LOCKED
    assert config.symbols == ("BTCUSDT", "ETHUSDT")
    assert config.mainnet_writes_enabled is False


@pytest.mark.parametrize(
    "payload",
    [
        {"mainnet_writes_enabled": True},
        {"allow_market_orders": True},
        {"symbols": ["SOLUSDT"]},
        {"mode": "TESTNET", "account_id": "binance-usdm:wrong"},
        {"mode": "PRODUCTION_LOCKED", "account_id": "binance-usdm-testnet:wrong"},
    ],
)
def test_config_rejects_unsafe_values(tmp_path, payload):
    with pytest.raises(ValueError):
        runtime_config_from_mapping({"hedge_r4": payload}, project_root=tmp_path)


def test_readiness_requires_every_required_component():
    aggregator = ReadinessAggregator(mode=R4Mode.PRODUCTION_LOCKED)
    aggregator.update(ComponentHealth("configuration", ComponentState.READY))
    snapshot = aggregator.snapshot(RuntimePhase.READY)
    assert not snapshot.ready
    assert any(item.startswith("database:") for item in snapshot.blockers)


def test_production_locked_readiness_never_allows_new_risk():
    aggregator = ReadinessAggregator(mode=R4Mode.PRODUCTION_LOCKED)
    for name in (
        "configuration", "database", "readonly_rest", "user_stream", "reconciliation",
        "planner", "risk", "execution", "control_plane", "supervisor",
    ):
        aggregator.update(ComponentHealth(name, ComponentState.READY))
    snapshot = aggregator.snapshot(RuntimePhase.READY)
    assert snapshot.ready
    assert snapshot.new_risk_allowed is False
