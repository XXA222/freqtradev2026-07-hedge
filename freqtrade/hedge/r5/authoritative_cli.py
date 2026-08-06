"""Explicit CLI for recovery, /order/test and authoritative plan execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

from freqtrade.hedge.execution.service import (
    IntentAction,
    OrderIntent,
    OrderType,
    PositionSide,
)

from .adoption import R5PositionAdoptionManifest
from .arming import R5StageArmAuthority
from .authoritative_mainnet import (
    R5MainnetCompositionRequest,
    build_r5_mainnet_execution_composition,
)
from .emergency import R5EmergencyExitAuthority
from .models import R5AccountSnapshot, R5Stage, default_limit_profile
from .plan_io import load_planning_result


def _secret(path: str) -> str:
    resolved = Path(path).expanduser().resolve(strict=True)
    lines = [
        line.strip()
        for line in resolved.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise ValueError(f"secret file must contain one non-empty line: {resolved}")
    return lines[0]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _budget_reservations(live, result):
    current = live.preflight.account
    long_notional = current.long_notional
    short_notional = current.short_notional
    open_orders = current.open_orders
    rows = []
    for item in result.submit_orders:
        before = R5AccountSnapshot(
            current.account_id,
            current.equity,
            long_notional,
            short_notional,
            open_orders,
            current.unmanaged_positions,
            current.observed_at,
        )
        notional = item.notional
        reduces = item.reduce_only
        if item.position_side.value == "LONG":
            long_notional = (
                max(Decimal("0"), long_notional - notional)
                if reduces
                else long_notional + notional
            )
        else:
            short_notional = (
                max(Decimal("0"), short_notional - notional)
                if reduces
                else short_notional + notional
            )
        open_orders += 1
        projected = R5AccountSnapshot(
            current.account_id,
            current.equity,
            long_notional,
            short_notional,
            open_orders,
            current.unmanaged_positions,
            current.observed_at,
        )
        rows.append((notional, projected, before, reduces))
    return tuple(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="freqtrade-hedge-r53-authoritative")
    parser.add_argument(
        "--mode",
        choices=("recover", "order-test", "execute-plan", "emergency-reduce"),
        required=True,
    )
    parser.add_argument("--credential-file", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument(
        "--stage",
        choices=[item.value for item in R5Stage if item.performs_exchange_write],
        required=True,
    )
    parser.add_argument("--actor", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--proxy-url")
    parser.add_argument("--r4-report", required=True)
    parser.add_argument("--r24-report", required=True)
    parser.add_argument("--user-stream-report", required=True)
    parser.add_argument("--evidence-max-age-seconds", type=int, default=900)
    parser.add_argument("--plan")
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"))
    parser.add_argument("--arm-token-file", required=True)
    parser.add_argument("--operator-token-file", required=True)
    parser.add_argument("--risk-token-file")
    parser.add_argument("--expected-arm-token-sha256", required=True)
    parser.add_argument("--expected-operator-token-sha256", required=True)
    parser.add_argument("--expected-risk-token-sha256")
    parser.add_argument("--confirmation", required=True)
    parser.add_argument("--adopt-position-leg", action="append", default=[])
    parser.add_argument("--adoption-manifest", action="append", default=[])
    parser.add_argument("--adoption-operator-token-file")
    parser.add_argument("--adoption-risk-token-file")
    parser.add_argument("--emergency-operator-token-file")
    parser.add_argument("--emergency-risk-token-file")
    parser.add_argument("--expected-emergency-operator-token-sha256")
    parser.add_argument("--expected-emergency-risk-token-sha256")
    parser.add_argument("--emergency-confirmation")
    parser.add_argument("--position-side", choices=("LONG", "SHORT"))
    parser.add_argument("--quantity")
    parser.add_argument("--reference-price")
    parser.add_argument("--reason")
    parser.add_argument("--allow-exchange-write", action="store_true")
    parser.add_argument("--confirmed", action="store_true")
    parser.add_argument("--output", required=True)
    return parser


def _load_adoption_manifests(paths: list[str]) -> tuple[R5PositionAdoptionManifest, ...]:
    values: list[R5PositionAdoptionManifest] = []
    for item in paths:
        path = Path(item).expanduser().resolve(strict=True)
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError(f"adoption manifest must be a JSON object: {path}")
        values.append(R5PositionAdoptionManifest.from_dict(payload))
    return tuple(values)

def _stage_permit(args: argparse.Namespace, composition, profile, stage):
    operator = _secret(args.operator_token_file)
    risk = None if args.risk_token_file is None else _secret(args.risk_token_file)
    if _hash(operator) != args.expected_operator_token_sha256.lower():
        raise PermissionError("operator token digest mismatch")
    if stage.can_create_position:
        if risk is None or args.expected_risk_token_sha256 is None:
            raise PermissionError("independent risk authorization is required")
        if _hash(risk) != args.expected_risk_token_sha256.lower():
            raise PermissionError("risk token digest mismatch")
    authority = R5StageArmAuthority(
        operator_hashes={stage: args.expected_operator_token_sha256.lower()},
        risk_hashes=(
            {}
            if args.expected_risk_token_sha256 is None
            else {stage: args.expected_risk_token_sha256.lower()}
        ),
    )
    return authority.issue(
        stage=stage,
        run_id=args.run_id,
        account_fingerprint=composition.preflight.account.account_id.split(":", 1)[1],
        actor=args.actor,
        operator_token=operator,
        risk_token=risk,
        confirmation=args.confirmation,
        profile=profile,
    )


def _emergency_intent(args: argparse.Namespace, composition, live):
    required = (
        args.emergency_operator_token_file,
        args.emergency_risk_token_file,
        args.expected_emergency_operator_token_sha256,
        args.expected_emergency_risk_token_sha256,
        args.emergency_confirmation,
        args.symbol,
        args.position_side,
        args.quantity,
        args.reference_price,
        args.reason,
    )
    if any(value in (None, "") for value in required):
        raise ValueError("all emergency-reduce arguments are required")
    quantity = Decimal(str(args.quantity))
    requested_reference = Decimal(str(args.reference_price))
    side = PositionSide(str(args.position_side))
    book = live.preflight.books[str(args.symbol)]
    reference = max(book.bid, book.ask, book.mark)
    if requested_reference <= 0:
        raise ValueError("emergency reference price must be positive")
    deviation = abs(requested_reference - reference) / reference
    if deviation > Decimal("0.02"):
        raise PermissionError("emergency reference price differs from live book by more than 2%")
    intent = OrderIntent(
        account_id=composition.preflight.account.account_id,
        symbol=str(args.symbol),
        position_side=side,
        action=IntentAction.CLOSE,
        quantity=quantity,
        idempotency_key=f"r53-emergency:{args.run_id}:{args.symbol}:{side.value}",
        order_type=OrderType.MARKET,
        reduce_only=True,
        metadata={
            "reference_price": str(reference),
            "bucket": "CORE",
            "emergency_reason": str(args.reason),
        },
    )
    emergency_operator = _secret(args.emergency_operator_token_file)
    emergency_risk = _secret(args.emergency_risk_token_file)
    operator_hash = str(args.expected_emergency_operator_token_sha256).lower()
    risk_hash = str(args.expected_emergency_risk_token_sha256).lower()
    if _hash(emergency_operator) != operator_hash:
        raise PermissionError("emergency operator token digest mismatch")
    if _hash(emergency_risk) != risk_hash:
        raise PermissionError("emergency risk token digest mismatch")
    authority = R5EmergencyExitAuthority(
        operator_sha256=operator_hash,
        risk_sha256=risk_hash,
    )
    permit = authority.issue(
        account_id=intent.account_id,
        symbol=intent.symbol,
        position_side=side,
        max_quantity=quantity,
        max_notional=quantity * reference,
        reason=str(args.reason),
        operator_token=emergency_operator,
        risk_token=emergency_risk,
        confirmation=str(args.emergency_confirmation),
    )
    current = live.preflight.account
    before = R5AccountSnapshot(
        current.account_id,
        current.equity,
        current.long_notional,
        current.short_notional,
        current.open_orders,
        current.unmanaged_positions,
        current.observed_at,
    )
    notional = quantity * reference
    projected = R5AccountSnapshot(
        current.account_id,
        current.equity,
        (
            max(Decimal("0"), current.long_notional - notional)
            if side is PositionSide.LONG
            else current.long_notional
        ),
        (
            max(Decimal("0"), current.short_notional - notional)
            if side is PositionSide.SHORT
            else current.short_notional
        ),
        current.open_orders + 1,
        current.unmanaged_positions,
        current.observed_at,
    )
    return intent, permit, ((notional, projected, before, True),)


def _run(args: argparse.Namespace) -> int:
    if not args.allow_exchange_write or not args.confirmed:
        raise PermissionError("explicit --allow-exchange-write and --confirmed are required")
    stage = R5Stage(args.stage)
    profile = default_limit_profile(stage)
    arm = _secret(args.arm_token_file)
    if _hash(arm) != args.expected_arm_token_sha256.lower():
        raise PermissionError("arm token digest mismatch")
    adoption_manifests = _load_adoption_manifests(args.adoption_manifest)
    adoption_operator = (
        None
        if args.adoption_operator_token_file is None
        else _secret(args.adoption_operator_token_file)
    )
    adoption_risk = (
        None
        if args.adoption_risk_token_file is None
        else _secret(args.adoption_risk_token_file)
    )
    composition = build_r5_mainnet_execution_composition(
        R5MainnetCompositionRequest(
            credential_file=args.credential_file,
            database_path=args.database,
            profile=profile,
            arm_token=arm,
            expected_arm_token_sha256=args.expected_arm_token_sha256,
            actor=args.actor,
            adopted_position_legs=tuple(args.adopt_position_leg),
            adoption_manifests=adoption_manifests,
            adoption_operator_token=adoption_operator,
            adoption_risk_token=adoption_risk,
            r4_report=args.r4_report,
            user_stream_report=args.user_stream_report,
            r24_report=args.r24_report,
            evidence_max_age_seconds=args.evidence_max_age_seconds,
            proxy_url=args.proxy_url,
            allow_market_orders=args.mode == "emergency-reduce",
        )
    )
    permit = _stage_permit(args, composition, profile, stage)
    try:
        if args.mode == "recover":
            report = composition.runtime.recover(symbols=tuple(composition.preflight.rules))
            payload = {
                "status": "PASS" if not report.unresolved else "RECOVERY_REQUIRED",
                "inspected": report.inspected,
                "recovered": len(report.recovered),
                "unresolved": list(report.unresolved),
                "fill_projection": asdict(report.fill_projection),
                "exchange_writes": 0,
            }
        elif args.mode == "order-test":
            if stage is not R5Stage.ORDER_TEST:
                raise PermissionError("order-test mode requires ORDER_TEST stage")
            if args.plan is None:
                raise ValueError("--plan is required")
            live = composition.refresh_live_evidence()
            _, planning = load_planning_result(args.plan, symbol=args.symbol)
            if len(planning.submit_orders) != 1:
                raise ValueError("ORDER_TEST requires exactly one submit order")
            from freqtrade.hedge.execution.planner_adapter import adapt_planner_intent

            intent = adapt_planner_intent(
                planning.submit_orders[0],
                account_id=live.preflight.account.account_id,
                strategy_id="R5.3.1_ORDER_TEST",
                cycle_id=args.run_id,
            )
            result = composition.runtime.validate_order_test(
                intent=intent,
                permit=permit,
                profile=profile,
            )
            payload = {"status": "PASS" if result.accepted else "FAILED", **asdict(result)}
        elif args.mode == "execute-plan":
            if args.plan is None:
                raise ValueError("--plan is required")
            live = composition.refresh_live_evidence()
            cycle_id, planning = load_planning_result(args.plan, symbol=args.symbol)
            report = composition.runtime.execute_planning_result(
                result=planning,
                portfolio=live.portfolio.portfolio,
                permit=permit,
                profile=profile,
                cycle_id=cycle_id,
                budget=composition.budget,
                budget_reservations=_budget_reservations(live, planning),
            )
            payload = {
                "status": "PASS",
                "cancellations": len(report.cancellations),
                "submissions": len(report.submissions),
                "writes_attempted": report.writes_attempted,
                "fencing_token": report.fencing_token,
                "fill_projection": asdict(report.fill_projection),
            }
        else:
            live = composition.refresh_live_evidence()
            intent, emergency_permit, reservations = _emergency_intent(args, composition, live)
            report = composition.runtime.execute_emergency_reduce(
                portfolio=live.portfolio.portfolio,
                intent=intent,
                emergency_permit=emergency_permit,
                stage_permit=permit,
                profile=profile,
                budget=composition.budget,
                budget_reservations=reservations,
            )
            payload = {
                "status": "PASS",
                "submissions": len(report.submissions),
                "writes_attempted": report.writes_attempted,
                "fencing_token": report.fencing_token,
                "fill_projection": asdict(report.fill_projection),
                "emergency": True,
            }
        output = Path(args.output).expanduser().resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
            encoding="ascii",
        )
        print(json.dumps(payload, sort_keys=True, default=str))
        return 0 if payload["status"] == "PASS" else 2
    finally:
        composition.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _run(args)
    except Exception as exc:
        payload = {
            "status": "FAILED",
            "error": str(exc),
            "exception_type": type(exc).__name__,
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
