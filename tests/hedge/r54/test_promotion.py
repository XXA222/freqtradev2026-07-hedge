from __future__ import annotations

from freqtrade.hedge.r5.models import R5Stage
from freqtrade.hedge.r54.evidence import create_envelope
from freqtrade.hedge.r54.models import ArtifactKind, CheckStatus, GateStatus
from freqtrade.hedge.r54.promotion import EVIDENCE_RULES, POLICIES, R54PromotionEngine
from freqtrade.hedge.r54.store import R54OperationalStore


def test_order_test_pending_until_all_external_evidence(tmp_path) -> None:
    store = R54OperationalStore(tmp_path / "r54.sqlite")
    engine = R54PromotionEngine(store)
    decision = engine.evaluate(account_id="acct", stage=R5Stage.ORDER_TEST)
    assert decision.status is GateStatus.PENDING_EXTERNAL_EVIDENCE
    assert len(decision.checks) == (
        len(POLICIES[R5Stage.MAINNET_READONLY].required)
        + len(POLICIES[R5Stage.ORDER_TEST].required)
    )


def test_order_test_ready_with_all_evidence(tmp_path) -> None:
    store = R54OperationalStore(tmp_path / "r54.sqlite")
    required = (
        *POLICIES[R5Stage.MAINNET_READONLY].required,
        *POLICIES[R5Stage.ORDER_TEST].required,
    )
    for kind in required:
        store.put_evidence(
            create_envelope(
                kind=kind,
                account_id="acct",
                environment=EVIDENCE_RULES[kind].environment,
                producer="test",
                payload={"kind": kind.value},
                status=CheckStatus.PASS,
                write_surface=next(iter(EVIDENCE_RULES[kind].write_surfaces)),
                real_order_requests=EVIDENCE_RULES[kind].minimum_requests,
            )
        )
    decision = R54PromotionEngine(store).evaluate(account_id="acct", stage=R5Stage.ORDER_TEST)
    assert decision.status is GateStatus.READY
    assert all(item.passed for item in decision.checks)


def test_failed_evidence_blocks_stage(tmp_path) -> None:
    store = R54OperationalStore(tmp_path / "r54.sqlite")
    store.put_evidence(
        create_envelope(
            kind=ArtifactKind.MAINNET_ORDER_TEST,
            account_id="acct",
            environment="MAINNET",
            producer="test",
            payload={"failed": True},
            status=CheckStatus.FAIL,
            write_surface="ORDER_TEST",
        )
    )
    decision = R54PromotionEngine(store).evaluate(account_id="acct", stage=R5Stage.SUBMIT_CANCEL)
    assert decision.status is GateStatus.BLOCKED


def test_environment_mismatch_blocks_promotion(tmp_path) -> None:
    store = R54OperationalStore(tmp_path / "r54.sqlite")
    store.put_evidence(
        create_envelope(
            kind=ArtifactKind.MAINNET_READONLY_PREFLIGHT,
            account_id="acct",
            environment="LOCAL",
            producer="test",
            payload={"wrong_environment": True},
            status=CheckStatus.PASS,
            write_surface="NONE",
        )
    )
    decision = R54PromotionEngine(store).evaluate(
        account_id="acct", stage=R5Stage.MAINNET_READONLY
    )
    assert decision.status is GateStatus.BLOCKED
    assert "environment" in decision.checks[0].reason
