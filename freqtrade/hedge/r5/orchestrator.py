from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol

from freqtrade.hedge.execution.service import IntentAction, PositionSide
from freqtrade.hedge.execution.state_machine import OrderState

from .arming import R5StageArmAuthority, R5StagePermit
from .auto import R5AutoSession
from .budget import R5DailyBudgetStore
from .config import R5Config
from .credentials import R5MainnetCredentials
from .evidence import R5EvidenceBundle, build_production_gate_evidence
from .executor import R5ExecutorPort, summarize_execution
from .journal import R5Journal
from .mainnet_api import R5LivePreflight
from .market import bounded_ioc_price, make_limit_intent, passive_price, quantity_for_notional
from .models import (
    R5AccountSnapshot,
    R5LimitProfile,
    R5RunState,
    R5Stage,
    R5StageReport,
    exact_decimal,
)
from .promotion import R5PromotionManifest, verify_promotion_manifest


class R5ReadApiPort(Protocol):
    def preflight(self) -> R5LivePreflight: ...
    def account_snapshot(self) -> R5AccountSnapshot: ...


ExecutorFactory = Callable[[R5LimitProfile, object, str, str], R5ExecutorPort]
AutoCycleFactory = Callable[[R5Stage, R5LimitProfile, R5ExecutorPort, R5StagePermit], Callable[[int, R5AccountSnapshot], Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class R5RunRequest:
    stage: R5Stage
    run_id: str
    actor: str
    symbol: str = "ETHUSDT"
    position_side: PositionSide = PositionSide.LONG
    target_notional: Decimal = Decimal("10")
    operator_token: str | None = None
    risk_token: str | None = None
    confirmation: str | None = None
    max_cycles: int = 10

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "run_id": self.run_id,
            "actor": self.actor,
            "symbol": self.symbol,
            "position_side": self.position_side.value,
            "target_notional": str(self.target_notional),
            "max_cycles": self.max_cycles,
        }

    @property
    def request_hash(self) -> str:
        raw = json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hashlib.sha256(raw).hexdigest()


class R5MainnetOrchestrator:
    def __init__(
        self,
        *,
        config: R5Config,
        credentials: R5MainnetCredentials,
        evidence: R5EvidenceBundle,
        read_api: R5ReadApiPort,
        journal: R5Journal,
        budget: R5DailyBudgetStore,
        executor_factory: ExecutorFactory,
        auto_cycle_factory: AutoCycleFactory | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials
        self.evidence = evidence
        self.read_api = read_api
        self.journal = journal
        self.budget = budget
        self.executor_factory = executor_factory
        self.auto_cycle_factory = auto_cycle_factory
        self.arm = R5StageArmAuthority(
            operator_hashes=dict(config.operator_token_sha256),
            risk_hashes=dict(config.risk_token_sha256),
        )

    def run(
        self,
        request: R5RunRequest,
        *,
        promotion_manifest: R5PromotionManifest | None = None,
        promotion_token: str | None = None,
    ) -> Mapping[str, Any]:
        started = datetime.now(UTC)
        stage = request.stage if isinstance(request.stage, R5Stage) else R5Stage(request.stage)
        profile = self.config.profiles[stage]
        target = exact_decimal(request.target_notional, "target_notional")
        if request.symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("R5 symbol is not allowlisted")
        if target <= 0 and stage is not R5Stage.MAINNET_READONLY:
            raise ValueError("R5 target_notional must be positive")
        record, replay = self.journal.claim(
            run_id=request.run_id,
            stage=stage,
            request_hash=request.request_hash,
            account_id=self.credentials.account_id,
            actor=request.actor,
            config=request.canonical_payload(),
        )
        if replay:
            if record.state is R5RunState.COMPLETED and record.report is not None:
                return record.report
            if record.state in {R5RunState.EXECUTING, R5RunState.VERIFYING, R5RunState.ARMED}:
                if record.state is not R5RunState.RECOVERY_REQUIRED:
                    self.journal.transition(
                        run_id=request.run_id,
                        target=R5RunState.RECOVERY_REQUIRED,
                        event="RESTART_REQUIRES_OPERATOR_RECOVERY",
                        error="R5 active run was interrupted; blind resubmission is prohibited",
                    )
                return self._terminal_report(
                    request, R5RunState.RECOVERY_REQUIRED, started,
                    checks={"blind_resubmission_blocked": True},
                    evidence={"previous_state": record.state.value},
                    error="interrupted R5 write-stage requires explicit recovery",
                )
            if record.report is not None:
                return record.report
            return self._terminal_report(
                request, record.state, started, checks={}, evidence={}, error=record.error
            )

        existing_stage_evidence = self.journal.stage_evidence(
            account_id=self.credentials.account_id, stage=stage
        )
        if existing_stage_evidence is not None and stage is not R5Stage.MAINNET_READONLY:
            self.journal.transition(
                run_id=request.run_id,
                target=R5RunState.BLOCKED,
                event="STAGE_ALREADY_COMPLETED",
                error="R5 stage already has durable completion evidence",
            )
            return self._terminal_report(
                request,
                R5RunState.BLOCKED,
                started,
                checks={"duplicate_stage_write_blocked": True},
                evidence={"existing_stage": stage.value},
                error="R5 stage already completed; replay the original run or perform an audited reset",
            )

        wrote = False
        executor: R5ExecutorPort | None = None
        try:
            self.journal.transition(run_id=request.run_id, target=R5RunState.PREFLIGHT, event="PREFLIGHT_STARTED")
            self.journal.assert_previous_complete(
                account_id=self.credentials.account_id,
                stage=stage,
                require_promotion_ready=stage is R5Stage.ORDER_TEST,
                max_age_seconds=(
                    self.config.evidence_max_age_seconds
                    if stage is R5Stage.ORDER_TEST
                    else None
                ),
            )
            preflight = self.read_api.preflight()
            self._assert_preflight(
                preflight,
                profile,
                stage=stage,
                enforce_stage_limits=stage is not R5Stage.MAINNET_READONLY,
            )
            if stage is R5Stage.MAINNET_READONLY:
                promotion_ready, promotion_blocks = self._readonly_promotion_readiness(preflight)
                self.journal.transition(run_id=request.run_id, target=R5RunState.EXECUTING, event="READONLY_FACTS_COLLECTED")
                self.journal.transition(run_id=request.run_id, target=R5RunState.VERIFYING, event="READONLY_VERIFYING")
                report = self._complete(
                    request, started,
                    checks={
                        **self._base_checks(preflight, include_promotion_policy=False),
                        "readonly_observation_complete": True,
                        "order_test_promotion_ready": promotion_ready,
                    },
                    evidence={
                        **self._preflight_evidence(preflight),
                        "order_test_promotion_ready": promotion_ready,
                        "order_test_promotion_block_reasons": promotion_blocks,
                    },
                    writes=0,
                )
                return report
            if profile.require_flat_start and not preflight.account.flat:
                raise PermissionError("R5 stage requires a flat starting account")

            permit = self.arm.issue(
                stage=stage,
                run_id=request.run_id,
                account_fingerprint=self.credentials.account_fingerprint,
                actor=request.actor,
                operator_token=request.operator_token or "",
                risk_token=request.risk_token,
                confirmation=request.confirmation or "",
                profile=profile,
            )
            if stage is R5Stage.EXPANDED_LIMITS:
                previous = self.journal.stage_evidence(account_id=self.credentials.account_id, stage=R5Stage.RESTRICTED_AUTO)
                if previous is None or promotion_manifest is None or promotion_token is None:
                    raise PermissionError("R5 expanded limits require signed promotion evidence")
                verify_promotion_manifest(
                    promotion_manifest,
                    expected_token_sha256=self.config.promotion_token_sha256,
                    promotion_token=promotion_token,
                    account_id=self.credentials.account_id,
                    previous_report=previous,
                    profile=profile,
                )
            self.journal.transition(run_id=request.run_id, target=R5RunState.ARMED, event="STAGE_ARMED")
            gate_evidence = build_production_gate_evidence(
                account_fingerprint=self.credentials.account_fingerprint,
                profile=profile,
                readonly_status=preflight.readonly_status,
                user_stream_status=self.evidence.user_stream_status,
                hedge_mode_enabled=preflight.hedge_mode_enabled,
                clock_offset_ms=preflight.clock_offset_ms,
                futures_trading_permission=preflight.futures_trading_permission,
                ip_restricted=preflight.ip_restricted,
                cross_margin_symbols=preflight.cross_margin_symbols,
                expected_arm_token_sha256=self.config.operator_token_sha256[stage],
            )
            executor = self.executor_factory(profile, gate_evidence, request.operator_token or "", request.actor)
            self.journal.transition(run_id=request.run_id, target=R5RunState.EXECUTING, event="EXECUTION_STARTED")
            result_evidence: dict[str, Any]
            if stage is R5Stage.ORDER_TEST:
                result_evidence = self._order_test(request, profile, preflight, permit, executor)
            elif stage is R5Stage.SUBMIT_CANCEL:
                wrote = True
                result_evidence = self._submit_cancel(request, profile, preflight, permit, executor)
            elif stage is R5Stage.SINGLE_LEG:
                wrote = True
                result_evidence = self._single_leg(request, profile, preflight, permit, executor)
            elif stage is R5Stage.DUAL_LEG:
                wrote = True
                result_evidence = self._dual_leg(request, profile, preflight, permit, executor)
            else:
                wrote = True
                result_evidence = self._automatic(request, profile, permit, executor)
            self.journal.transition(run_id=request.run_id, target=R5RunState.VERIFYING, event="FINAL_VERIFICATION_STARTED")
            final_snapshot = self.read_api.account_snapshot()
            self._assert_snapshot(final_snapshot, profile)
            if profile.require_flat_finish and not final_snapshot.flat:
                raise RuntimeError("R5 final account is not flat")
            telemetry = executor.telemetry()
            result_evidence.update(
                {
                    "final_gross_notional": str(final_snapshot.gross_notional),
                    "final_net_notional": str(final_snapshot.net_notional),
                    "final_open_orders": final_snapshot.open_orders,
                    "adapter_write_requests": telemetry.write_requests,
                    "adapter_read_requests": telemetry.read_requests,
                    "remaining_stage_write_budget": permit.remaining_writes,
                }
            )
            return self._complete(
                request,
                started,
                checks={**self._base_checks(preflight), "stage_sequence": True, "final_account_limits": True},
                evidence={**self._preflight_evidence(preflight), **result_evidence},
                writes=telemetry.write_requests,
            )
        except Exception as exc:
            target_state = R5RunState.RECOVERY_REQUIRED if wrote else R5RunState.FAILED
            current = self.journal.require(request.run_id)
            if not current.state.terminal:
                try:
                    self.journal.transition(
                        run_id=request.run_id,
                        target=target_state,
                        event="RUN_FAILED",
                        error=f"{type(exc).__name__}:{exc}",
                    )
                except RuntimeError:
                    target_state = R5RunState.FAILED
            report = self._terminal_report(
                request, target_state, started,
                checks={"blind_resubmission_blocked": True},
                evidence={"exception_type": type(exc).__name__},
                error=str(exc),
                writes=0 if executor is None else executor.telemetry().write_requests,
            )
            return report
        finally:
            if executor is not None and hasattr(executor, "disarm"):
                try:
                    executor.disarm()  # type: ignore[attr-defined]
                except Exception:
                    pass
            self.arm.revoke_all()

    def _order_test(self, request, profile, preflight, permit, executor):
        rules = preflight.rules[request.symbol]
        book = preflight.books[request.symbol]
        price = passive_price(book=book, rules=rules, position_side=request.position_side, action=IntentAction.OPEN)
        qty = quantity_for_notional(rules=rules, price=price, target_notional=request.target_notional, maximum_notional=profile.max_single_order_notional)
        intent = make_limit_intent(account_id=self.credentials.account_id, stage=request.stage, run_id=request.run_id, symbol=request.symbol, position_side=request.position_side, action=IntentAction.OPEN, quantity=qty, price=price, time_in_force="GTX", sequence=1)
        self._persist_intent(request.run_id, intent, "ORDER_TEST")
        permit.consume("ORDER_TEST")
        result = executor.validate(intent)
        return {"test_order_accepted": result.accepted, "client_order_id": result.client_order_id, "writes_attempted": 1}

    def _submit_cancel(self, request, profile, preflight, permit, executor):
        rules = preflight.rules[request.symbol]
        book = preflight.books[request.symbol]
        price = passive_price(book=book, rules=rules, position_side=request.position_side, action=IntentAction.OPEN)
        qty = quantity_for_notional(rules=rules, price=price, target_notional=request.target_notional, maximum_notional=profile.max_single_order_notional)
        intent = make_limit_intent(account_id=self.credentials.account_id, stage=request.stage, run_id=request.run_id, symbol=request.symbol, position_side=request.position_side, action=IntentAction.OPEN, quantity=qty, price=price, time_in_force="GTX", sequence=1)
        self.budget.reserve_order(account_id=self.credentials.account_id, profile=profile, order_notional=qty * price, projected=preflight.account)
        self._persist_intent(request.run_id, intent, "SUBMIT")
        permit.consume("SUBMIT")
        submitted = executor.submit(intent)
        if submitted.order.lifecycle.status is OrderState.UNKNOWN or submitted.order.lifecycle.filled_quantity > 0:
            raise RuntimeError("R5 submit-cancel order was ambiguous or unexpectedly filled")
        permit.consume("CANCEL")
        canceled = executor.cancel(submitted.order.client_order_id)
        if canceled.order.lifecycle.status is not OrderState.CANCELED or canceled.order.lifecycle.filled_quantity != 0:
            raise RuntimeError("R5 submit-cancel did not finish canceled with zero fill")
        return {"submit": self._summary(submitted), "cancel": self._summary(canceled), "writes_attempted": 2}

    def _single_leg(self, request, profile, preflight, permit, executor):
        return self._open_and_close(request, profile, preflight, permit, executor, request.position_side, sequence=1)

    def _dual_leg(self, request, profile, preflight, permit, executor):
        first = self._open_only(
            request, profile, preflight, permit, executor, PositionSide.LONG, sequence=1
        )
        second = None
        close_short = None
        close_long = None
        primary_error = None
        cleanup_errors: list[str] = []
        try:
            second = self._open_only(
                request, profile, preflight, permit, executor, PositionSide.SHORT, sequence=3
            )
            close_short = self._close_fill(
                request, profile, permit, executor, second, PositionSide.SHORT, sequence=4
            )
        except Exception as exc:
            primary_error = exc
        finally:
            try:
                close_long = self._close_fill(
                    request, profile, permit, executor, first, PositionSide.LONG, sequence=2
                )
            except Exception as exc:
                cleanup_errors.append(f"LONG:{type(exc).__name__}:{exc}")
            if second is not None and close_short is None:
                try:
                    close_short = self._close_fill(
                        request, profile, permit, executor, second, PositionSide.SHORT, sequence=4
                    )
                except Exception as exc:
                    cleanup_errors.append(f"SHORT:{type(exc).__name__}:{exc}")
        if primary_error is not None or cleanup_errors:
            detail = []
            if primary_error is not None:
                detail.append(f"PRIMARY:{type(primary_error).__name__}:{primary_error}")
            detail.extend(cleanup_errors)
            raise RuntimeError("R5 dual-leg canary required cleanup: " + " | ".join(detail))
        if second is None or close_short is None or close_long is None:
            raise RuntimeError("R5 dual-leg canary did not produce complete evidence")
        return {
            "long_open": self._summary(first),
            "short_open": self._summary(second),
            "short_close": self._summary(close_short),
            "long_close": self._summary(close_long),
            "writes_attempted": 4,
        }

    def _open_and_close(self, request, profile, preflight, permit, executor, side, sequence):
        opened = self._open_only(request, profile, preflight, permit, executor, side, sequence=sequence)
        closed = self._close_fill(request, profile, permit, executor, opened, side, sequence=sequence + 1)
        return {"open": self._summary(opened), "close": self._summary(closed), "writes_attempted": 2}

    def _open_only(self, request, profile, preflight, permit, executor, side, sequence):
        rules = preflight.rules[request.symbol]
        book = preflight.books[request.symbol]
        price = bounded_ioc_price(book=book, rules=rules, position_side=side, action=IntentAction.OPEN, max_slippage_bps=profile.max_slippage_bps)
        qty = quantity_for_notional(rules=rules, price=price, target_notional=request.target_notional, maximum_notional=profile.max_single_order_notional)
        current_snapshot = self.read_api.account_snapshot()
        projected = self._project(current_snapshot, side, qty * price, add=True)
        self.budget.reserve_order(account_id=self.credentials.account_id, profile=profile, order_notional=qty * price, projected=projected)
        intent = make_limit_intent(account_id=self.credentials.account_id, stage=request.stage, run_id=request.run_id, symbol=request.symbol, position_side=side, action=IntentAction.OPEN, quantity=qty, price=price, time_in_force="IOC", sequence=sequence)
        self._persist_intent(request.run_id, intent, "OPEN")
        permit.consume("OPEN")
        result = executor.submit(intent)
        if result.order.lifecycle.status is OrderState.UNKNOWN:
            raise RuntimeError("R5 open order remains UNKNOWN")
        if result.order.lifecycle.filled_quantity <= 0:
            raise RuntimeError("R5 open order produced no fill")
        return result

    def _close_fill(self, request, profile, permit, executor, opened, side, sequence):
        quantity = opened.order.lifecycle.filled_quantity
        book = self.read_api.preflight().books[request.symbol]
        rules = self.read_api.preflight().rules[request.symbol]
        price = bounded_ioc_price(book=book, rules=rules, position_side=side, action=IntentAction.CLOSE, max_slippage_bps=profile.max_slippage_bps)
        snapshot = self.read_api.account_snapshot()
        projected = self._project(snapshot, side, quantity * price, add=False)
        self.budget.reserve_order(account_id=self.credentials.account_id, profile=profile, order_notional=quantity * price, projected=projected)
        intent = make_limit_intent(account_id=self.credentials.account_id, stage=request.stage, run_id=request.run_id, symbol=request.symbol, position_side=side, action=IntentAction.CLOSE, quantity=quantity, price=price, time_in_force="IOC", sequence=sequence)
        self._persist_intent(request.run_id, intent, "CLOSE")
        permit.consume("CLOSE")
        result = executor.submit(intent)
        if result.order.lifecycle.status is OrderState.UNKNOWN:
            raise RuntimeError("R5 close order remains UNKNOWN")
        if result.order.lifecycle.filled_quantity != quantity:
            raise RuntimeError("R5 close order did not fully flatten the canary quantity")
        return result

    def _automatic(self, request, profile, permit, executor):
        if self.auto_cycle_factory is None:
            raise PermissionError("R5 automatic execution requires an explicitly configured strategy bridge")
        session = R5AutoSession(
            profile=profile,
            budget=self.budget,
            account_id=self.credentials.account_id,
            snapshot_provider=self.read_api.account_snapshot,
            cycle=self.auto_cycle_factory(request.stage, profile, executor, permit),
        )
        return session.run(max_cycles=request.max_cycles).to_dict()

    def _persist_intent(self, run_id, intent, operation):
        from freqtrade.hedge.execution.client_order_id import build_client_order_id
        client_id = build_client_order_id(account_id=intent.account_id, symbol=intent.symbol, position_side=intent.position_side.value, idempotency_key=intent.idempotency_key)
        self.journal.append_event(run_id=run_id, event="ORDER_INTENT_PERSISTED", details={"operation": operation, "client_order_id": client_id, "symbol": intent.symbol, "position_side": intent.position_side.value, "action": intent.action.value, "quantity": str(intent.quantity), "limit_price": str(intent.limit_price), "time_in_force": str(intent.metadata.get("time_in_force"))})

    def _assert_preflight(
        self,
        value,
        profile,
        *,
        stage: R5Stage,
        enforce_stage_limits: bool = True,
    ):
        if value.readonly_status != "FULL_PASS" or not value.hedge_mode_enabled or not value.single_asset_mode:
            raise PermissionError("R5 mainnet preflight failed")
        if abs(value.clock_offset_ms) > 1000:
            raise PermissionError("R5 clock offset exceeds limit")
        if tuple(value.cross_margin_symbols) != ("BTCUSDT", "ETHUSDT"):
            raise PermissionError("R5 Cross-margin evidence is incomplete")
        if self.evidence.r24_account_fingerprint:
            candidates = self._live_evidence_candidates(value)
            if self.evidence.r24_account_fingerprint not in candidates:
                raise PermissionError("R5 live account does not match R2.4/R2.5 evidence")
        self._assert_snapshot_fresh(value.account)
        if stage is not R5Stage.MAINNET_READONLY:
            if self.config.require_reading_permission and not value.api_reading_permission:
                raise PermissionError("R5 API reading permission is required")
            if self.config.require_futures_permission and not value.futures_trading_permission:
                raise PermissionError("R5 Futures trading permission is required")
            if self.config.require_ip_restriction and not value.ip_restricted:
                raise PermissionError("R5 API IP restriction is required")
            if self.config.require_withdrawals_disabled and not value.withdrawals_disabled:
                raise PermissionError("R5 API key must not allow withdrawals")
            if self.config.require_account_can_trade and not value.account_can_trade:
                raise PermissionError("R5 account trading is disabled")
        if enforce_stage_limits:
            self._assert_snapshot(value.account, profile)

    @staticmethod
    def _assert_snapshot_fresh(snapshot):
        if (datetime.now(UTC) - snapshot.observed_at).total_seconds() > 30:
            raise PermissionError("R5 account snapshot is stale")

    def _readonly_promotion_readiness(self, preflight):
        blocks: list[str] = []
        if self.config.require_reading_permission and not preflight.api_reading_permission:
            blocks.append("READING_PERMISSION_DISABLED")
        if self.config.require_futures_permission and not preflight.futures_trading_permission:
            blocks.append("FUTURES_TRADING_PERMISSION_DISABLED")
        if self.config.require_ip_restriction and not preflight.ip_restricted:
            blocks.append("IP_RESTRICTION_DISABLED")
        if self.config.require_withdrawals_disabled and not preflight.withdrawals_disabled:
            blocks.append("WITHDRAWAL_PERMISSION_ENABLED")
        if self.config.require_account_can_trade and not preflight.account_can_trade:
            blocks.append("ACCOUNT_TRADING_DISABLED")
        snapshot = preflight.account
        if not snapshot.flat:
            blocks.append("ACCOUNT_NOT_FLAT")
        if snapshot.open_orders != 0:
            blocks.append("ACTIVE_ORDERS_PRESENT")
        if snapshot.unmanaged_positions:
            blocks.append("UNMANAGED_EXPOSURE_PRESENT")
        return not blocks, blocks

    def _assert_snapshot(self, snapshot, profile):
        if self.config.require_zero_unmanaged_positions and snapshot.unmanaged_positions:
            raise PermissionError("R5 unmanaged positions or orders are present")
        if snapshot.gross_notional > profile.max_gross_notional or snapshot.net_notional > profile.max_net_notional:
            raise PermissionError("R5 account exposure exceeds stage limit")
        if snapshot.open_orders > profile.max_open_orders:
            raise PermissionError("R5 open-order count exceeds stage limit")

    @staticmethod
    def _summary(result):
        value = summarize_execution(result)
        return {
            "client_order_id": value.client_order_id,
            "status": value.status,
            "filled_quantity": str(value.filled_quantity),
            "average_price": None if value.average_price is None else str(value.average_price),
            "idempotent_replay": value.idempotent_replay,
        }

    @staticmethod
    def _project(snapshot, side, notional, *, add):
        amount = exact_decimal(notional, "notional")
        long_value = snapshot.long_notional
        short_value = snapshot.short_notional
        if side is PositionSide.LONG:
            long_value = long_value + amount if add else max(Decimal("0"), long_value - amount)
        else:
            short_value = short_value + amount if add else max(Decimal("0"), short_value - amount)
        return R5AccountSnapshot(snapshot.account_id, snapshot.equity, long_value, short_value, snapshot.open_orders, snapshot.unmanaged_positions, datetime.now(UTC))

    def _base_checks(self, preflight, *, include_promotion_policy: bool = True):
        checks = {
            "mainnet_readonly": True,
            "hedge_mode": preflight.hedge_mode_enabled,
            "single_asset_mode": preflight.single_asset_mode,
            "cross_btc_eth": tuple(preflight.cross_margin_symbols) == ("BTCUSDT", "ETHUSDT"),
            "clock_within_limit": abs(preflight.clock_offset_ms) <= 1000,
            "evidence_account_bound": self._live_evidence_bound(preflight),
        }
        if include_promotion_policy:
            checks.update({
                "reading_permission": preflight.api_reading_permission,
                "withdrawals_disabled": preflight.withdrawals_disabled,
                "futures_permission": preflight.futures_trading_permission,
                "account_can_trade": preflight.account_can_trade,
                "ip_restricted": preflight.ip_restricted,
            })
        return checks

    @staticmethod
    def _live_evidence_candidates(preflight) -> tuple[str, ...]:
        candidates = tuple(
            str(item).strip()
            for item in getattr(preflight, "evidence_account_fingerprint_candidates", ())
            if str(item).strip()
        )
        primary = str(getattr(preflight, "evidence_account_fingerprint", "")).strip()
        if primary and primary not in candidates:
            candidates = (primary, *candidates)
        return candidates

    def _live_evidence_bound(self, preflight) -> bool:
        expected = str(self.evidence.r24_account_fingerprint or "").strip()
        return bool(expected) and expected in self._live_evidence_candidates(preflight)

    def _live_evidence_binding_mode(self, preflight) -> str:
        expected = str(self.evidence.r24_account_fingerprint or "").strip()
        candidates = tuple(
            str(item).strip()
            for item in getattr(preflight, "evidence_account_fingerprint_candidates", ())
            if str(item).strip()
        )
        modes = tuple(
            str(item).strip()
            for item in getattr(preflight, "evidence_account_fingerprint_modes", ())
        )
        for index, candidate in enumerate(candidates):
            if candidate == expected:
                return modes[index] if index < len(modes) and modes[index] else "LEGACY_COMPATIBLE"
        primary = str(getattr(preflight, "evidence_account_fingerprint", "")).strip()
        if primary == expected:
            return "PRIMARY_COMPATIBLE"
        return "UNBOUND"

    def _preflight_evidence(self, preflight):
        return {
            "account_fingerprint": self.credentials.account_fingerprint,
            "clock_offset_ms": preflight.clock_offset_ms,
            "readonly_requests": preflight.request_count,
            "r4_report_sha256": self.evidence.r4_sha256,
            "user_stream_report_sha256": self.evidence.user_stream_sha256,
            "user_stream_status": self.evidence.user_stream_status,
            "r24_report_sha256": self.evidence.r24_sha256,
            "r24_account_fingerprint": self.evidence.r24_account_fingerprint,
            "stream_account_fingerprint": self.evidence.stream_account_fingerprint,
            "live_evidence_account_fingerprint": (
                self.evidence.r24_account_fingerprint
                if self._live_evidence_bound(preflight)
                else preflight.evidence_account_fingerprint
            ),
            "live_evidence_binding_mode": self._live_evidence_binding_mode(preflight),
            "live_evidence_candidate_count": len(self._live_evidence_candidates(preflight)),
            "account_alias_present": bool(getattr(preflight, "account_alias_present", False)),
            "starting_equity": str(preflight.account.equity),
            "starting_long_notional": str(preflight.account.long_notional),
            "starting_short_notional": str(preflight.account.short_notional),
            "starting_gross_notional": str(preflight.account.gross_notional),
            "starting_net_notional": str(preflight.account.net_notional),
            "starting_open_orders": preflight.account.open_orders,
            "unmanaged_positions": list(preflight.account.unmanaged_positions),
            "account_can_trade": bool(getattr(preflight, "account_can_trade", False)),
            "api_reading_permission": bool(getattr(preflight, "api_reading_permission", False)),
            "account_alias_sha256": hashlib.sha256(
                str(getattr(preflight, "account_alias", "")).encode("utf-8")
            ).hexdigest() if getattr(preflight, "account_alias", "") else None,
            "managed_position_legs": list(getattr(preflight, "managed_position_legs", ())),
            "active_order_summaries": list(getattr(preflight, "active_order_summaries", ())),
            "balance_assets": list(getattr(preflight, "balance_assets", ())),
            "mainnet_rest_base_url": self.config.mainnet_rest_base_url,
            "sapi_base_url": self.config.sapi_base_url,
            "readonly_client_get_only": True,
        }

    def _complete(self, request, started, *, checks, evidence, writes):
        report = R5StageReport(request.run_id, request.stage, R5RunState.COMPLETED, self.credentials.account_id, request.actor, checks, evidence, writes, writes, started, datetime.now(UTC)).to_dict()
        self.journal.complete_with_evidence(
            run_id=request.run_id,
            report=report,
            replace_stage_evidence=request.stage is R5Stage.MAINNET_READONLY,
        )
        return report

    def _terminal_report(self, request, state, started, *, checks, evidence, error, writes=0):
        report = R5StageReport(request.run_id, request.stage, state, self.credentials.account_id, request.actor, checks, evidence, writes, writes, started, datetime.now(UTC), error).to_dict()
        return report
