import json

from tools.hedge_r40.r40_toolkit.acceptance import build_acceptance_report


def test_offline_acceptance_is_complete(tmp_path):
    previous = tmp_path / "r381.json"
    previous.write_text(json.dumps({
        "status": "PRODUCTION_RUNTIME_CLOSED_LOOP_COMPLETE_MAINNET_LOCKED",
        "production_runtime_closed_loop_complete": True,
        "mainnet_live_exchange_write": "LOCKED",
        "real_mainnet_order_requests_sent": 0,
    }), encoding="utf-8")
    output = tmp_path / "acceptance.json"
    payload = build_acceptance_report(output=output, r381_report=previous)
    assert payload["status"] == "R4_FULL_COMPLETION_COMPLETE_MAINNET_LOCKED"
    assert payload["r4_full_completion_complete"] is True
    assert payload["required_failures"] == []
    assert output.is_file()
