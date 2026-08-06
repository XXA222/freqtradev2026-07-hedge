"""Production composition root for Binance USD-M authoritative execution.

No caller may construct a live Binance write adapter without passing through this
module.  It binds live preflight evidence, durable SQL authority, account-wide risk,
single-writer fencing, revocable stage authorization, budget reservations, recovery,
and fill/income projection into one graph.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from freqtrade.hedge.concurrency.database_lease import SqliteDatabaseLeaseStore
from freqtrade.hedge.concurrency.position_lock import PositionLockManager
from freqtrade.hedge.concurrency.single_writer import SingleWriterGuard
from freqtrade.hedge.execution.binance_environment import ExecutionEnvironment
from freqtrade.hedge.execution.binance_usdm_adapter import BinanceUSDMExecutionAdapter
from freqtrade.hedge.execution.production_gate import (
    ProductionExecutionGate,
    ProductionGateEvidence,
)
from freqtrade.hedge.readiness.checks import ReadinessInputs
from freqtrade.hedge.risk.commit import SqlRiskApprovalCommitStore
from freqtrade.hedge.risk.limits import RiskLimits
from freqtrade.hedge.risk.runtime import HedgeRiskRuntime, build_hedge_risk_runtime
from freqtrade.persistence.hedge_models import create_hedge_tables
from freqtrade.hedge.execution.state_machine import OrderState
from freqtrade.hedge.exchange.shared_rate_limit import SqliteSharedWeightBudget

from .adoption import (
    R5PositionAdoptionManifest,
    apply_position_adoption_manifest,
)
from .authoritative_runtime import R5AuthoritativeRuntime, build_r5_authoritative_runtime
from .budget import R5BudgetPort
from .credentials import (
    R5MainnetCredentials,
    assert_credential_file_security,
    load_mainnet_credentials,
)
from .fill_closed_loop import R5SqlFillProjector
from .fill_processor import R5InventoryProcessor
from .income_closed_loop import R5IncomeProjector
from .mainnet_api import R5LivePreflight, R5MainnetReadApi
from .mainnet_portfolio import R5PortfolioEvidence, build_live_risk_portfolio
from .models import R5LimitProfile, R5Stage
from .sql_budget import SqlR5DailyBudgetStore
from .strategy_store import R5StrategyStateStore
from .strategy_shadow import facts_from_preflight
from .user_stream_evidence import R5UserStreamEvidence


@dataclass(frozen=True, slots=True)
class R5LiveExecutionEvidence:
    preflight: R5LivePreflight
    portfolio: R5PortfolioEvidence


@dataclass(frozen=True, slots=True)
class R5MainnetExecutionComposition:
    runtime: R5AuthoritativeRuntime
    preflight: R5LivePreflight
    portfolio: R5PortfolioEvidence
    budget: R5BudgetPort
    income: R5IncomeProjector
    read_api: R5MainnetReadApi
    gate: ProductionExecutionGate
    writer: SingleWriterGuard
    risk_runtime: HedgeRiskRuntime
    user_stream_evidence: R5UserStreamEvidence
    recovery_report: object
    session_factory: object
    database_path: str
    adopted_position_legs: tuple[str, ...]
    strategy_store: R5StrategyStateStore
    engine: object

    def refresh_live_evidence(self) -> R5LiveExecutionEvidence:
        """Refresh REST facts and atomically update the runtime readiness monitor."""
        self.read_api.synchronize_clock()
        latest = self.read_api.preflight()
        if latest.account.account_id != self.preflight.account.account_id:
            raise PermissionError("R5 live account identity changed")
        portfolio = build_live_risk_portfolio(
            preflight=latest,
            ownership=self.runtime.ownership,
            adopted_position_legs=self.adopted_position_legs,
        )
        unknown_count = _unknown_order_count(self.runtime.store)
        inputs = _readiness(
            preflight=latest,
            portfolio=portfolio,
            writer=self.writer,
            user_stream=self.user_stream_evidence,
            unknown_order_count=unknown_count,
            reconciliation_converged=(
                self.user_stream_evidence.reconciliation_converged and unknown_count == 0
            ),
        )
        report = self.risk_runtime.readiness.update(**asdict(inputs))
        if not report.ready:
            reasons = ",".join(item.value for item in report.reason_codes)
            raise PermissionError(f"R5 dynamic readiness blocked execution: {reasons}")
        return R5LiveExecutionEvidence(latest, portfolio)

    def close(self) -> None:
        """Release writer, SQLite and SQLAlchemy resources deterministically."""
        try:
            self.gate.disarm()
        except Exception:
            pass
        try:
            self.risk_runtime.stop(release_lease=True)
        except Exception:
            pass
        try:
            self.strategy_store.close()
        except Exception:
            pass
        dispose = getattr(self.engine, "dispose", None)
        if callable(dispose):
            dispose()


@dataclass(frozen=True, slots=True)
class R5MainnetCompositionRequest:
    credential_file: str
    database_path: str
    profile: R5LimitProfile
    arm_token: str
    expected_arm_token_sha256: str
    actor: str
    adopted_position_legs: tuple[str, ...] = ()
    adoption_manifests: tuple[R5PositionAdoptionManifest, ...] = ()
    adoption_operator_token: str | None = None
    adoption_risk_token: str | None = None
    r4_report: str = ""
    user_stream_report: str = ""
    r24_report: str = ""
    evidence_max_age_seconds: int = 900
    proxy_url: str | None = None
    allow_market_orders: bool = False
    arm_ttl_seconds: int = 300
    legacy_credentials_readonly_only: bool = False

    def __post_init__(self) -> None:
        if self.profile.stage is R5Stage.MAINNET_READONLY:
            raise ValueError("authoritative write composition requires a write stage")
        if not str(self.arm_token):
            raise ValueError("arm_token is required")
        digest = str(self.expected_arm_token_sha256).strip().lower()
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("expected_arm_token_sha256 is invalid")
        if hashlib.sha256(self.arm_token.encode("utf-8")).hexdigest() != digest:
            raise PermissionError("arm token does not match configured digest")
        if not str(self.actor).strip():
            raise ValueError("actor is required")
        for name in ("r4_report", "user_stream_report", "r24_report"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required for authoritative writes")
        if (
            isinstance(self.evidence_max_age_seconds, bool)
            or not isinstance(self.evidence_max_age_seconds, int)
            or not 30 <= self.evidence_max_age_seconds <= 3600
        ):
            raise ValueError("evidence_max_age_seconds must be in [30, 3600]")
        if self.adopted_position_legs and not self.adoption_manifests:
            raise PermissionError("position adoption requires signed adoption manifests")
        if self.adoption_manifests and (
            not self.adoption_operator_token or not self.adoption_risk_token
        ):
            raise PermissionError("position adoption requires independent verification tokens")


def _risk_limits(profile: R5LimitProfile) -> RiskLimits:
    return RiskLimits(
        max_margin_utilization=Decimal("0.80"),
        min_liquidation_buffer_ratio=Decimal("0.05"),
        max_gross_notional=max(profile.max_gross_notional, Decimal("0.00000001")),
        max_single_order_notional=max(profile.max_single_order_notional, Decimal("0.00000001")),
        max_leg_notional=max(profile.max_gross_notional, Decimal("0.00000001")),
        max_symbol_gross_notional=max(profile.max_gross_notional, Decimal("0.00000001")),
        max_net_notional=max(profile.max_net_notional, Decimal("0.00000001")),
        max_pending_order_notional=max(profile.max_gross_notional, Decimal("0.00000001")),
        min_available_balance=Decimal("0"),
    )


def _readiness(
    *,
    preflight: R5LivePreflight,
    portfolio: R5PortfolioEvidence,
    writer: SingleWriterGuard,
    user_stream: R5UserStreamEvidence,
    unknown_order_count: int,
    reconciliation_converged: bool,
) -> ReadinessInputs:
    observed_at_ms = int(preflight.account.observed_at.timestamp() * 1000)
    leverage_values = tuple(
        preflight.leverage_by_symbol[symbol]
        for symbol in sorted(preflight.leverage_by_symbol)
    )
    configured = leverage_values[0] if leverage_values else Decimal("1")
    leverage_consistent = leverage_values and all(item == configured for item in leverage_values)
    return ReadinessInputs(
        database_migration_succeeded=True,
        database_migration_checksum_valid=True,
        single_writer_lease_valid=writer.status().valid,
        position_mode="hedge",
        margin_mode="cross",
        configured_leverage=configured,
        observed_leverages=leverage_values or (configured,),
        unmanaged_position_count=len(portfolio.unmanaged_position_legs),
        unmanaged_order_count=(
            len(portfolio.external_client_order_ids)
            + len(portfolio.orphan_prefix_client_order_ids)
        ),
        rest_snapshot_valid=preflight.readonly_status == "FULL_PASS",
        rest_snapshot_observed_at_ms=observed_at_ms,
        rest_snapshot_max_age_ms=90_000,
        user_stream_fresh=(user_stream.stream_connected and user_stream.stream_fresh),
        user_stream_observed_at_ms=int(user_stream.observed_at.timestamp() * 1000),
        user_stream_max_age_ms=90_000,
        user_stream_gap_detected=user_stream.gap_detected,
        unknown_order_count=unknown_order_count,
        reconciliation_converged=reconciliation_converged,
        risk_data_valid=(
            preflight.hedge_mode_enabled
            and preflight.single_asset_mode
            and bool(leverage_consistent)
            and not portfolio.unmanaged_position_legs
        ),
        risk_snapshot_observed_at_ms=observed_at_ms,
        risk_snapshot_max_age_ms=90_000,
    )



def _unknown_order_count(store: object) -> int:
    list_orders = getattr(store, "list_orders", None)
    if not callable(list_orders):
        raise TypeError("authoritative execution store cannot enumerate orders")
    unresolved = {OrderState.PREPARED, OrderState.SUBMITTING, OrderState.UNKNOWN}
    return sum(1 for order in list_orders() if order.lifecycle.status in unresolved)

def build_r5_mainnet_execution_composition(
    request: R5MainnetCompositionRequest,
) -> R5MainnetExecutionComposition:
    """Build and arm the complete LIVE graph; no order is sent by construction."""

    credential_path = Path(request.credential_file).expanduser().resolve(strict=True)
    # Real writes require the marked three-line format and restrictive file permissions.
    credentials: R5MainnetCredentials = load_mainnet_credentials(
        credential_path,
        allow_legacy_readonly=request.legacy_credentials_readonly_only,
    )
    if credentials.source_format == "LEGACY_READONLY":
        raise PermissionError("legacy two-line credentials are readonly-only")
    assert_credential_file_security(credential_path, for_exchange_write=True)
    user_stream_evidence = R5UserStreamEvidence.load(
        r4_report=request.r4_report,
        user_stream_report=request.user_stream_report,
        r24_report=request.r24_report,
        max_age_seconds=request.evidence_max_age_seconds,
    )
    if user_stream_evidence.account_fingerprint != credentials.account_fingerprint:
        raise PermissionError("R5 user-stream evidence account binding mismatch")

    database_path = Path(request.database_path).expanduser().resolve(strict=False)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}", future=True)
    create_hedge_tables(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    shared_weight = SqliteSharedWeightBudget(
        database_path,
        limit_per_minute=2400,
        reserve_weight=200,
    )
    read_api = R5MainnetReadApi(
        credentials=credentials,
        proxy_url=request.proxy_url,
        shared_weight_budget=shared_weight,
    )
    preflight = read_api.preflight()
    if preflight.account.account_id != credentials.account_id:
        raise PermissionError("R5 live account binding mismatch")
    if not preflight.api_reading_permission:
        raise PermissionError("R5 API reading permission is required")

    lease_store = SqliteDatabaseLeaseStore(
        database_path,
        table_name="hedge_r5_single_writer_lease",
    )
    writer = SingleWriterGuard(
        lease_store,
        owner_id=f"r5-authoritative-{request.actor}-{uuid4().hex[:8]}",
        lease_name=f"r5-authoritative:{credentials.account_id}",
        ttl_ms=15_000,
    )
    writer.acquire()
    locks = PositionLockManager()

    state_store = R5StrategyStateStore(database_path)
    inventory = R5InventoryProcessor(state_store)
    sql_budget = SqlR5DailyBudgetStore(sessions)
    fill_projector = R5SqlFillProjector(
        session_factory=sessions,
        inventory=inventory,
        budget=sql_budget,
        profile=request.profile,
    )

    verified_adopted_legs: set[str] = set()
    for manifest in request.adoption_manifests:
        facts = facts_from_preflight(
            preflight,
            symbol=manifest.symbol,
            store=state_store,
            observed_at=preflight.account.observed_at,
        )
        apply_position_adoption_manifest(
            store=state_store,
            manifest=manifest,
            facts=facts,
            expected_account_fingerprint=credentials.account_fingerprint,
            operator_token=str(request.adoption_operator_token),
            risk_token=str(request.adoption_risk_token),
            now=preflight.account.observed_at,
        )
        if manifest.long_quantity > 0:
            verified_adopted_legs.add(f"{manifest.symbol}:LONG")
        if manifest.short_quantity > 0:
            verified_adopted_legs.add(f"{manifest.symbol}:SHORT")
    requested_adopted = set(request.adopted_position_legs)
    if requested_adopted and requested_adopted != verified_adopted_legs:
        raise PermissionError("requested position adoption scope does not match signed manifests")

    # Bootstrap the SQL store first so live ownership is never inferred by prefix.
    from freqtrade.persistence.hedge_execution_adapters import SqlExecutionStore

    bootstrap_store = SqlExecutionStore(sessions)
    from freqtrade.hedge.execution.ownership import ExecutionOrderOwnershipRegistry

    portfolio = build_live_risk_portfolio(
        preflight=preflight,
        ownership=ExecutionOrderOwnershipRegistry(bootstrap_store),
        adopted_position_legs=tuple(sorted(verified_adopted_legs)),
    )
    readiness = _readiness(
        preflight=preflight,
        portfolio=portfolio,
        writer=writer,
        user_stream=user_stream_evidence,
        unknown_order_count=_unknown_order_count(bootstrap_store),
        reconciliation_converged=user_stream_evidence.reconciliation_converged,
    )
    risk_runtime = build_hedge_risk_runtime(
        limits=_risk_limits(request.profile),
        writer=writer,
        readiness_inputs=readiness,
        position_locks=locks,
        enable_lease_runner=True,
    )
    risk_runtime.start()

    arm_digest = request.expected_arm_token_sha256
    gate = ProductionExecutionGate(
        ProductionGateEvidence(
            environment=ExecutionEnvironment.LIVE,
            account_fingerprint=credentials.account_fingerprint,
            allowed_symbols=tuple(sorted(preflight.rules)),
            cross_margin_symbols=preflight.cross_margin_symbols,
            readonly_status=preflight.readonly_status,
            user_stream_status=user_stream_evidence.status,
            hedge_mode_enabled=preflight.hedge_mode_enabled,
            clock_offset_ms=preflight.clock_offset_ms,
            live_trading_enabled=True,
            strict_key_policy_passed=(
                preflight.futures_trading_permission
                and preflight.ip_restricted
                and preflight.withdrawals_disabled
                and preflight.account_can_trade
            ),
            futures_trading_permission=preflight.futures_trading_permission,
            ip_restricted=preflight.ip_restricted,
            expected_arm_token_sha256=arm_digest,
            max_order_notional=max(request.profile.max_single_order_notional, Decimal("0.00000001")),
            allow_market_orders=request.allow_market_orders,
            account_id_prefix="binance-usdm",
        )
    )
    gate.arm(
        token=request.arm_token,
        actor=request.actor,
        confirmed=True,
        ttl_seconds=request.arm_ttl_seconds,
    )

    runtime = build_r5_authoritative_runtime(
        account_id=credentials.account_id,
        session_factory=sessions,
        writer=writer,
        position_locks=locks,
        risk_runtime=risk_runtime,
        risk_commit_store=SqlRiskApprovalCommitStore(sessions),
        exchange_factory=lambda store: BinanceUSDMExecutionAdapter(
            credentials=credentials.execution_credentials(),
            gate=gate,
            store=store,
            proxy_url=request.proxy_url,
            shared_weight_budget=shared_weight,
        ),
        fill_projector=fill_projector,
        leverage=(
            next(iter(preflight.leverage_by_symbol.values()))
            if preflight.leverage_by_symbol
            else Decimal("1")
        ),
        maintenance_margin_rate=Decimal("0.01"),
        budget=sql_budget,
    )
    adopted = verified_adopted_legs
    for row in preflight.managed_position_legs:
        symbol = str(row.get("symbol", "")).upper()
        side = str(row.get("position_side", "")).upper()
        quantity = abs(Decimal(str(row.get("position_amount", "0"))))
        if quantity <= 0 or f"{symbol}:{side}" not in adopted:
            continue
        runtime.seed_position_projection(
            symbol=symbol,
            position_side=side,
            quantity=quantity,
            average_entry_price=Decimal(str(row.get("entry_price", "0"))),
            observed_at=preflight.account.observed_at,
        )

    adapter = runtime.exchange.delegate
    synchronize = getattr(adapter, "synchronize_clock", None)
    if callable(synchronize):
        offset = int(synchronize())
        if abs(offset) > 1000:
            raise PermissionError("R5 execution clock offset exceeds 1000 ms")

    income = R5IncomeProjector(budget=sql_budget, profile=request.profile)
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    income.project(
        account_id=credentials.account_id,
        rows=read_api.income_history(start_time_ms=int(day_start.timestamp() * 1000)),
    )
    recovery = runtime.recover(symbols=tuple(sorted(preflight.rules)))
    if recovery.unresolved:
        risk_runtime.readiness.update(
            unknown_order_count=len(recovery.unresolved),
            reconciliation_converged=False,
            halt_reasons=("R5_RECOVERY_UNRESOLVED",),
        )
        raise PermissionError(
            "R5 recovery contains unresolved orders: " + ",".join(recovery.unresolved)
        )

    read_api.synchronize_clock()
    refreshed_preflight = read_api.preflight()
    if refreshed_preflight.account.account_id != credentials.account_id:
        raise PermissionError("R5 account identity changed after recovery")
    refreshed_portfolio = build_live_risk_portfolio(
        preflight=refreshed_preflight,
        ownership=runtime.ownership,
        adopted_position_legs=tuple(sorted(verified_adopted_legs)),
    )
    refreshed_readiness = _readiness(
        preflight=refreshed_preflight,
        portfolio=refreshed_portfolio,
        writer=writer,
        user_stream=user_stream_evidence,
        unknown_order_count=_unknown_order_count(runtime.store),
        reconciliation_converged=user_stream_evidence.reconciliation_converged,
    )
    final_report = risk_runtime.readiness.update(**asdict(refreshed_readiness))
    if not final_report.ready:
        reasons = ",".join(item.value for item in final_report.reason_codes)
        raise PermissionError(f"R5 composition readiness failed after recovery: {reasons}")

    return R5MainnetExecutionComposition(
        runtime=runtime,
        preflight=refreshed_preflight,
        portfolio=refreshed_portfolio,
        budget=sql_budget,
        income=income,
        read_api=read_api,
        gate=gate,
        writer=writer,
        risk_runtime=risk_runtime,
        user_stream_evidence=user_stream_evidence,
        recovery_report=recovery,
        session_factory=sessions,
        database_path=str(database_path),
        adopted_position_legs=tuple(sorted(verified_adopted_legs)),
        strategy_store=state_store,
        engine=engine,
    )
