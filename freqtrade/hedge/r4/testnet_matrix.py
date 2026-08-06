from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from freqtrade.hedge.execution.service import PositionSide
from freqtrade.hedge.execution.testnet_e2e import (
    TestnetE2EConfig,
    TestnetScenarioMode,
    TestnetScenarioState,
)


class TestnetMatrixOrchestrator(Protocol):
    async def run(self, **kwargs: Any) -> object: ...


@dataclass(frozen=True, slots=True)
class TestnetMatrixCase:
    case_id: str
    mode: TestnetScenarioMode
    symbol: str
    position_side: PositionSide
    target_notional: Decimal = Decimal("10")

    def __post_init__(self) -> None:
        if self.symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("R4 Testnet matrix supports only BTCUSDT and ETHUSDT")
        if not self.case_id.strip():
            raise ValueError("Testnet matrix case_id cannot be empty")
        if not self.target_notional.is_finite() or not Decimal("0") < self.target_notional <= Decimal("25"):
            raise ValueError("Testnet target notional must be in (0, 25]")


@dataclass(frozen=True, slots=True)
class TestnetMatrixCaseResult:
    case: TestnetMatrixCase
    passed: bool
    report: object | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TestnetMatrixReport:
    started_at: datetime
    completed_at: datetime
    results: tuple[TestnetMatrixCaseResult, ...]

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(item.passed for item in self.results)

    @property
    def failures(self) -> tuple[TestnetMatrixCaseResult, ...]:
        return tuple(item for item in self.results if not item.passed)


class R4TestnetMatrixRunner:
    """Run all requested Testnet cases and never stop at the first failure."""

    def __init__(self, orchestrator: TestnetMatrixOrchestrator) -> None:
        self.orchestrator = orchestrator

    @staticmethod
    def cases(*, include_submit_cancel: bool) -> tuple[TestnetMatrixCase, ...]:
        values: list[TestnetMatrixCase] = []
        for symbol in ("BTCUSDT", "ETHUSDT"):
            for side in (PositionSide.LONG, PositionSide.SHORT):
                values.append(
                    TestnetMatrixCase(
                        case_id=f"validation-{symbol.lower()}-{side.value.lower()}",
                        mode=TestnetScenarioMode.VALIDATION_ONLY,
                        symbol=symbol,
                        position_side=side,
                    )
                )
        if include_submit_cancel:
            for side in (PositionSide.LONG, PositionSide.SHORT):
                values.append(
                    TestnetMatrixCase(
                        case_id=f"submit-cancel-ethusdt-{side.value.lower()}",
                        mode=TestnetScenarioMode.SUBMIT_CANCEL,
                        symbol="ETHUSDT",
                        position_side=side,
                    )
                )
        return tuple(values)

    async def run(
        self,
        *,
        credentials: object,
        arm_token: str,
        actor: str,
        execution_transport: object,
        include_submit_cancel: bool = False,
        run_prefix: str = "r4-matrix",
    ) -> TestnetMatrixReport:
        normalized_actor = str(actor).strip()
        normalized_prefix = str(run_prefix).strip()
        if not normalized_actor:
            raise ValueError("Testnet matrix actor cannot be empty")
        if not normalized_prefix or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for ch in normalized_prefix):
            raise ValueError("Testnet matrix run_prefix is invalid")
        if include_submit_cancel and len(str(arm_token)) < 16:
            raise ValueError("submit-cancel matrix requires an arm token of at least 16 characters")
        started = datetime.now(UTC)
        results: list[TestnetMatrixCaseResult] = []
        for case in self.cases(include_submit_cancel=include_submit_cancel):
            try:
                report = await self.orchestrator.run(
                    credentials=credentials,
                    config=TestnetE2EConfig(
                        mode=case.mode,
                        symbol=case.symbol,
                        position_side=case.position_side,
                        target_notional=case.target_notional,
                    ),
                    arm_token=arm_token,
                    actor=normalized_actor,
                    run_id=f"{normalized_prefix}-{case.case_id}",
                    execution_transport=execution_transport,
                )
                final_state = getattr(report, "final_state", None)
                status = str(getattr(report, "status", ""))
                passed = final_state is TestnetScenarioState.COMPLETED and status in {
                    "TESTNET_E2E_COMPLETE",
                    "TESTNET_EXECUTION_COMPLETE",
                }
                results.append(
                    TestnetMatrixCaseResult(
                        case=case,
                        passed=passed,
                        report=report,
                        error=None if passed else f"unexpected terminal result: {status} {final_state}",
                    )
                )
            except Exception as exc:
                results.append(
                    TestnetMatrixCaseResult(
                        case=case,
                        passed=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return TestnetMatrixReport(started, datetime.now(UTC), tuple(results))
