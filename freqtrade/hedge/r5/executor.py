from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from freqtrade.hedge.execution.binance_usdm_adapter import (
    BinanceExecutionCredentials,
    BinanceExecutionTelemetry,
    BinanceTestOrderValidation,
    BinanceUSDMExecutionAdapter,
    HttpTransport,
)
from freqtrade.hedge.execution.client_order_id import build_client_order_id
from freqtrade.hedge.execution.idempotency import InMemoryIdempotencyStore
from freqtrade.hedge.execution.kill_switch import KillSwitch
from freqtrade.hedge.execution.production_gate import ProductionExecutionGate
from freqtrade.hedge.execution.service import (
    ApprovedOrderIntent,
    ExecutionResult,
    InMemoryExecutionStore,
    IntentAction,
    OrderIntent,
    OrderType,
    RiskApproval,
)
from freqtrade.hedge.execution.unknown_resolver import UnknownOrderResolver

from .credentials import R5MainnetCredentials
from .models import R5LimitProfile


class R5ExecutorPort(Protocol):
    def validate(self, intent: OrderIntent) -> BinanceTestOrderValidation: ...
    def submit(self, intent: OrderIntent) -> ExecutionResult: ...
    def cancel(self, client_order_id: str) -> ExecutionResult: ...
    def query(self, client_order_id: str) -> ExecutionResult | None: ...
    def telemetry(self) -> BinanceExecutionTelemetry: ...


@dataclass(frozen=True, slots=True)
class R5ExecutionSummary:
    client_order_id: str
    status: str
    filled_quantity: Decimal
    average_price: Decimal | None
    idempotent_replay: bool


class _R5RiskApproval:
    def __init__(self, profile: R5LimitProfile) -> None:
        self.profile = profile

    def approve(self, intent: OrderIntent) -> RiskApproval:
        if intent.order_type is not OrderType.LIMIT:
            return RiskApproval(False, Decimal("0"), ("R5_MARKET_DISABLED",))
        if intent.limit_price is None:
            return RiskApproval(False, Decimal("0"), ("R5_LIMIT_PRICE_REQUIRED",))
        notional = intent.quantity * intent.limit_price
        if notional > self.profile.max_single_order_notional:
            return RiskApproval(False, Decimal("0"), ("R5_SINGLE_ORDER_LIMIT",))
        tif = str(intent.metadata.get("time_in_force", "")).upper()
        if tif not in {"GTX", "IOC", "GTC"}:
            return RiskApproval(False, Decimal("0"), ("R5_TIF_NOT_ALLOWED",))
        if tif == "GTC" and not intent.action.reduces_risk:
            return RiskApproval(False, Decimal("0"), ("R5_RISK_INCREASING_GTC_DISABLED",))
        return RiskApproval(True, intent.quantity, ("R5_LIMITS_PASS",))


class R5OrderExecutor:
    """Concrete production executor reusing the hardened R3 execution service.

    The gate remains process-local. Every new process must independently arm with the
    configured operator token. UNKNOWN results are queried before any possible retry.
    """

    def __init__(
        self,
        *,
        credentials: R5MainnetCredentials,
        profile: R5LimitProfile,
        evidence,
        operator_token: str,
        actor: str,
        proxy_url: str | None = None,
        transport: HttpTransport | None = None,
        ttl_seconds: int = 300,
    ) -> None:
        self.credentials = credentials
        self.profile = profile
        self.store = InMemoryExecutionStore()
        self.gate = ProductionExecutionGate(evidence)
        self.gate.arm(
            token=operator_token,
            actor=actor,
            confirmed=True,
            ttl_seconds=ttl_seconds,
        )
        self.adapter = BinanceUSDMExecutionAdapter(
            credentials=BinanceExecutionCredentials(
                credentials.api_key,
                credentials.api_secret,
            ),
            gate=self.gate,
            store=self.store,
            proxy_url=proxy_url,
            base_url="https://fapi.binance.com",
            transport=transport,
        )
        resolver = UnknownOrderResolver(self.adapter)
        from freqtrade.hedge.execution.service import ExecutionService

        self.service = ExecutionService(
            risk=_R5RiskApproval(profile),
            exchange=self.adapter,
            store=self.store,
            idempotency=InMemoryIdempotencyStore(),
            unknown_resolver=resolver,
            kill_switch=KillSwitch(),
        )
        self.adapter.synchronize_clock()

    def validate(self, intent: OrderIntent) -> BinanceTestOrderValidation:
        approved = self._approved(intent)
        return self.adapter.validate_order(approved)

    def submit(self, intent: OrderIntent) -> ExecutionResult:
        return self.service.submit(intent)

    def cancel(self, client_order_id: str) -> ExecutionResult:
        return self.service.cancel(client_order_id)

    def query(self, client_order_id: str) -> ExecutionResult | None:
        order = self.store.get_by_client_order_id(client_order_id)
        if order is None:
            return None
        return self.service.refresh_order(client_order_id)

    def telemetry(self) -> BinanceExecutionTelemetry:
        return self.adapter.telemetry()

    def disarm(self) -> None:
        self.gate.disarm()

    def _approved(self, intent: OrderIntent) -> ApprovedOrderIntent:
        approval = _R5RiskApproval(self.profile).approve(intent)
        if not approval.approved:
            raise PermissionError(",".join(approval.reason_codes))
        client_id = build_client_order_id(
            account_id=intent.account_id,
            symbol=intent.symbol,
            position_side=intent.position_side.value,
            idempotency_key=intent.idempotency_key,
        )
        return ApprovedOrderIntent(
            intent=intent,
            approved_quantity=approval.approved_quantity,
            client_order_id=client_id,
            approved_at=datetime.now(UTC),
            risk_reason_codes=approval.reason_codes,
        )


def summarize_execution(result: ExecutionResult) -> R5ExecutionSummary:
    lifecycle = result.order.lifecycle
    return R5ExecutionSummary(
        client_order_id=result.order.client_order_id,
        status=lifecycle.status.value,
        filled_quantity=lifecycle.filled_quantity,
        average_price=lifecycle.average_price,
        idempotent_replay=result.idempotent_replay,
    )
