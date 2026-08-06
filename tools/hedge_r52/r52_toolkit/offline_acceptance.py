from __future__ import annotations

import argparse
import ast
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from freqtrade.hedge.planning.context import MarketSnapshot, PlannerConfig, PositionBucket, PositionSide
from freqtrade.hedge.r5.adoption import build_position_adoption_manifest, verify_position_adoption_manifest
from freqtrade.hedge.r5.fill_processor import (
    R5InventoryEvent,
    R5InventoryEventType,
    R5InventoryProcessor,
)
from freqtrade.hedge.r5.models import R5Stage, default_limit_profile
from freqtrade.hedge.r5.position_ledger import R5CoreTacticalLedger
from freqtrade.hedge.r5.strategy_runtime import (
    R5ProductionStrategyRuntime,
    R5StrategyFacts,
    R5StrategyMode,
    R5StrategySignals,
)
from freqtrade.hedge.r5.strategy_session import R5StrategySession, R5StrategySessionConfig
from freqtrade.hedge.r5.strategy_store import R5StrategyStateStore


D = Decimal


def _facts(now: datetime) -> R5StrategyFacts:
    return R5StrategyFacts(
        account_id="binance-usdm:offline",
        symbol="ETHUSDT",
        observed_at=now,
        market=MarketSnapshot(
            "ETHUSDT", now, D("99"), D("101"), D("100"),
            tick_size=D("0.1"), qty_step=D("0.01"), min_qty=D("0.01"), min_notional=D("5"),
        ),
        balance=D("1000"),
        equity=D("1000"),
        available_balance=D("1000"),
        leverage=D("10"),
        long_quantity=D("0"),
        long_average_price=D("0"),
        short_quantity=D("0"),
        short_average_price=D("0"),
    )


def _config() -> PlannerConfig:
    return PlannerConfig(
        core_wallet_exposure_long=D("0.03"),
        core_wallet_exposure_short=D("0.03"),
        tactical_wallet_exposure_long=D("0.02"),
        tactical_wallet_exposure_short=D("0.02"),
        max_wallet_exposure_long=D("0.08"),
        max_wallet_exposure_short=D("0.08"),
        max_gross_wallet_exposure=D("0.10"),
        max_grid_layers=2,
        max_pending_entries=2,
        cooldown_seconds=0,
        max_single_order_notional=D("25"),
    )


def build(project_root: str | Path, output: str | Path) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve(strict=True)
    paths = {
        "runtime": root / "freqtrade/hedge/r5/strategy_runtime.py",
        "store": root / "freqtrade/hedge/r5/strategy_store.py",
        "ledger": root / "freqtrade/hedge/r5/position_ledger.py",
        "adoption": root / "freqtrade/hedge/r5/adoption.py",
        "fill_processor": root / "freqtrade/hedge/r5/fill_processor.py",
        "session": root / "freqtrade/hedge/r5/strategy_session.py",
        "budget": root / "freqtrade/hedge/r5/budget.py",
        "shadow": root / "freqtrade/hedge/r5/strategy_shadow.py",
        "mainnet_api": root / "freqtrade/hedge/r5/mainnet_api.py",
        "close_grid": root / "freqtrade/hedge/planning/close_grid.py",
        "planning_context": root / "freqtrade/hedge/planning/context.py",
        "unstuck": root / "freqtrade/hedge/planning/unstuck.py",
        "r4_planner": root / "freqtrade/hedge/r4/planner.py",
        "tests": root / "tools/hedge_r52/tests/test_r52_production_strategy_core.py",
    }
    checks: dict[str, bool] = {f"file_{name}": path.is_file() for name, path in paths.items()}
    for name, path in paths.items():
        if path.is_file() and path.suffix == ".py":
            try:
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                checks[f"ast_{name}"] = True
            except (OSError, UnicodeError, SyntaxError):
                checks[f"ast_{name}"] = False

    runtime_text = paths["runtime"].read_text(encoding="utf-8")
    shadow_text = paths["shadow"].read_text(encoding="utf-8")
    toolkit_text = (root / "tools/hedge_r52/r52_toolkit/__main__.py").read_text(encoding="utf-8")
    checks.update(
        {
            "runtime_modes_present": all(item in runtime_text for item in ("SHADOW", "DRY_RUN", "TESTNET", "MAINNET_LOCKED", "MAINNET_LIMITED")),
            "write_modes_require_executor": "write mode requires an executor" in runtime_text,
            "nonwrite_modes_reject_executor": "non-write mode must not receive an executor" in runtime_text,
            "external_orders_block_live": "blocked by external active orders" in runtime_text,
            "unmanaged_positions_block_live": "blocked by unmanaged positions" in runtime_text,
            "position_adoption_required_live": "requires explicit position adoption policy" in runtime_text,
            "plan_persisted_before_execution": runtime_text.index("persist_plan") < runtime_text.index("begin_execution"),
            "reduce_orders_sorted_first": "not item.reduce_only" in runtime_text,
            "unknown_submit_recovery": "submission status is UNKNOWN" in runtime_text,
            "unknown_cancel_recovery": "cancellation status is UNKNOWN" in runtime_text,
            "market_orders_absent": "OrderType.MARKET" not in runtime_text,
            "production_unstuck_limit_policy": "unstuck_limit_only=True" in paths["r4_planner"].read_text(encoding="utf-8"),
            "unstuck_limit_gtc_generation": all(
                token in paths["unstuck"].read_text(encoding="utf-8")
                for token in (
                    "OrderType.LIMIT if cfg.unstuck_limit_only",
                    "TimeInForce.GTC if cfg.unstuck_limit_only",
                )
            ),
            "unstuck_notional_cap_before_quantization": all(
                token in paths["unstuck"].read_text(encoding="utf-8")
                for token in ("quantity_cap", "cfg.max_single_order_notional / price")
            ),
            "unstuck_policy_is_typed_boolean": (
                "unstuck_limit_only: bool = False"
                in paths["planning_context"].read_text(encoding="utf-8")
            ),
            "real_account_gross_shadow_regression": (
                "test_mainnet_locked_high_gross_unstuck_is_aggressive_limit_and_notional_capped"
                in paths["tests"].read_text(encoding="utf-8")
            ),
            "shadow_has_no_order_endpoint": all(token not in shadow_text + toolkit_text for token in ("/fapi/v1/order", "/fapi/v1/order/test", "DELETE /fapi")),
            "shadow_reports_zero_mainnet_writes": '"real_mainnet_order_requests_sent": 0' in toolkit_text,
            "state_store_uses_wal": "PRAGMA journal_mode=WAL" in paths["store"].read_text(encoding="utf-8"),
            "state_store_uses_begin_immediate": "BEGIN IMMEDIATE" in paths["store"].read_text(encoding="utf-8"),
            "adoption_dual_signatures": all(item in paths["adoption"].read_text(encoding="utf-8") for item in ("operator_signature", "risk_signature")),
            "close_grid_caps_reduce_notional": "_cap_close_quantity" in paths["close_grid"].read_text(encoding="utf-8"),
            "mainnet_api_exposes_order_identity": all(item in paths["mainnet_api"].read_text(encoding="utf-8") for item in ("client_order_id", "order_id", "leverage_by_symbol")),
            "write_budget_checked_before_executor": runtime_text.index("required_writes") < runtime_text.index("self.executor.cancel"),
            "daily_budget_batch_reserved_before_executor": runtime_text.index("reserve_orders") < runtime_text.index("self.executor.cancel"),
            "mainnet_limited_requires_promotion_journal": "promotion journal" in runtime_text,
            "managed_cancel_status_persisted": "update_managed_order_status" in runtime_text,
            "inventory_events_idempotent": "apply_inventory_event" in paths["store"].read_text(encoding="utf-8"),
            "inventory_tables_present": all(item in paths["store"].read_text(encoding="utf-8") for item in ("r52_inventory_legs", "r52_inventory_events")),
            "session_is_bounded": all(item in paths["session"].read_text(encoding="utf-8") for item in ("max_cycles", "max_runtime_seconds", "kill_switch")),
            "shadow_external_count_not_doubled": "external_for_symbol + other_external" not in shadow_text,
        }
    )

    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="r52-offline-") as directory:
        store = R5StrategyStateStore(Path(directory) / "strategy.sqlite")
        runtime = R5ProductionStrategyRuntime(
            account_id="binance-usdm:offline",
            symbol="ETHUSDT",
            mode=R5StrategyMode.MAINNET_LOCKED,
            profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
            planner_config=_config(),
            store=store,
        )
        result = runtime.run_cycle(
            cycle_id="offline-shadow-cycle",
            facts=_facts(now),
            signals=R5StrategySignals(long_signal=D("1"), short_signal=D("1")),
        )
        checks["shadow_cycle_pass"] = result.status == "PASS"
        checks["shadow_cycle_zero_writes"] = result.writes_attempted == 0
        checks["shadow_cycle_dual_leg"] = {
            item["position_side"] for item in result.report["planner"]["ideal_orders"]
        } == {"LONG", "SHORT"}
        high_gross = R5StrategyFacts(
            account_id="binance-usdm:offline",
            symbol="ETHUSDT",
            observed_at=now,
            market=MarketSnapshot(
                "ETHUSDT", now, D("1865.17"), D("1865.18"), D("1865.175"),
                tick_size=D("0.01"), qty_step=D("0.001"), min_qty=D("0.001"), min_notional=D("5"),
            ),
            balance=D("1723.5"),
            equity=D("1723.5"),
            available_balance=D("100"),
            leverage=D("10"),
            long_quantity=D("10"),
            long_average_price=D("1776.45"),
            short_quantity=D("10"),
            short_average_price=D("1725.15"),
            external_order_count=5,
        )
        high_gross_runtime = R5ProductionStrategyRuntime(
            account_id="binance-usdm:offline",
            symbol="ETHUSDT",
            mode=R5StrategyMode.MAINNET_LOCKED,
            profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
            planner_config=_config(),
            store=R5StrategyStateStore(Path(directory) / "high-gross.sqlite"),
        )
        high_gross_result = high_gross_runtime.run_cycle(
            cycle_id="offline-real-account-gross",
            facts=high_gross,
            signals=R5StrategySignals(),
        )
        high_gross_unstuck = [
            item for item in high_gross_result.report["planner"]["ideal_orders"]
            if item["action"] == "UNSTUCK"
        ]
        checks["high_gross_shadow_unstuck_pass"] = (
            high_gross_result.status == "PASS"
            and high_gross_result.writes_attempted == 0
            and {item["position_side"] for item in high_gross_unstuck} == {"LONG", "SHORT"}
        )
        checks["high_gross_unstuck_limit_gtc"] = all(
            item["order_type"] == "LIMIT" and item["time_in_force"] == "GTC"
            for item in high_gross_unstuck
        )
        checks["high_gross_unstuck_notional_capped"] = all(
            D(item["notional"]) <= D("25") for item in high_gross_unstuck
        )
        checks["shadow_cycle_durable_replay"] = runtime.run_cycle(
            cycle_id="offline-shadow-cycle",
            facts=_facts(now),
            signals=R5StrategySignals(long_signal=D("1"), short_signal=D("1")),
        ).plan_sha256 == result.plan_sha256
        checks["strategy_database_integrity"] = store.integrity()["status"] == "PASS"

        long_leg = R5CoreTacticalLedger.adopt_as_core(
            side=PositionSide.LONG, quantity=D("1"), average_price=D("100")
        )
        long_leg = R5CoreTacticalLedger.open_fill(
            long_leg,
            bucket=PositionBucket.TACTICAL,
            quantity=D("1"),
            price=D("90"),
            tactical_lot_id="offline-lot",
        )
        long_leg = R5CoreTacticalLedger.reduce_fill(
            long_leg,
            bucket=PositionBucket.TACTICAL,
            quantity=D("1"),
            price=D("110"),
        )
        checks["tactical_pnl_accounting"] = long_leg.tactical_realized_pnl == D("20") and long_leg.core_quantity == D("1")

        manifest = build_position_adoption_manifest(
            facts=_facts(now),
            account_fingerprint="0123456789abcdef",
            operator_token="operator-token-123456789",
            risk_token="risk-token-1234567890",
            now=now,
        )
        digest = verify_position_adoption_manifest(
            manifest,
            facts=_facts(now),
            expected_account_fingerprint="0123456789abcdef",
            operator_token="operator-token-123456789",
            risk_token="risk-token-1234567890",
            now=now,
        )
        checks["adoption_manifest_verified"] = digest == manifest.payload_sha256

        inventory_store = R5StrategyStateStore(Path(directory) / "inventory.sqlite")
        inventory_store.adopt_core_baseline(
            account_id="binance-usdm:offline",
            symbol="ETHUSDT",
            long_quantity=D("1"),
            long_average_price=D("100"),
            short_quantity=D("0"),
            short_average_price=D("0"),
            manifest_sha256="d" * 64,
        )
        processor = R5InventoryProcessor(inventory_store)
        inventory_event = R5InventoryEvent(
            event_id="offline-fill-1",
            account_id="binance-usdm:offline",
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            event_type=R5InventoryEventType.OPEN_FILL,
            quantity=D("0.5"),
            price=D("90"),
            bucket=PositionBucket.TACTICAL,
            tactical_lot_id="offline-fill-lot",
            occurred_at=now,
        )
        inventory, replay = processor.apply(inventory_event)
        repeated, replay_second = processor.apply(inventory_event)
        checks["inventory_fill_applied"] = inventory.tactical_quantity == D("0.5")
        checks["inventory_fill_idempotent"] = not replay and replay_second and repeated == inventory

        session_runtime = R5ProductionStrategyRuntime(
            account_id="binance-usdm:offline",
            symbol="ETHUSDT",
            mode=R5StrategyMode.MAINNET_LOCKED,
            profile=default_limit_profile(R5Stage.RESTRICTED_AUTO),
            planner_config=_config(),
            store=R5StrategyStateStore(Path(directory) / "session.sqlite"),
        )
        session_report = R5StrategySession(
            runtime=session_runtime,
            facts_provider=lambda index: _facts(datetime.now(UTC)),
            signals_provider=lambda index, facts: R5StrategySignals(
                long_signal=D("1"), short_signal=D("1")
            ),
            config=R5StrategySessionConfig(
                session_id="offline-session",
                max_cycles=2,
                max_runtime_seconds=10,
                cycle_interval_seconds=0,
            ),
            output=Path(directory) / "session-report.json",
        ).run()
        checks["bounded_session_pass"] = (
            session_report["status"] == "COMPLETED"
            and session_report["cycles_completed"] == 2
            and session_report["writes_attempted"] == 0
        )

    failures = sorted(name for name, passed in checks.items() if not passed)
    payload = {
        "schema_version": 1,
        "toolkit_version": "5.2.3",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "R5_2_PRODUCTION_STRATEGY_CORE_OFFLINE_PASS" if not failures else "R5_2_PRODUCTION_STRATEGY_CORE_OFFLINE_FAIL",
        "checks": {name: "PASS" if passed else "FAIL" for name, passed in sorted(checks.items())},
        "check_count": len(checks),
        "required_failures": failures,
        "installer_exchange_access": "NONE",
        "real_mainnet_order_requests_sent": 0,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    target = Path(output).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="ascii")
    temporary.replace(target)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hedge-r52-offline")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    payload = build(args.project_root, args.output)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    return 0 if not payload["required_failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
