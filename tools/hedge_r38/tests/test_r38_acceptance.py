from __future__ import annotations

import json
from pathlib import Path

from r38_toolkit.acceptance import build_acceptance_report


def test_acceptance_is_complete_and_mainnet_locked(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    report = tmp_path / "r37.json"
    report.write_text(
        json.dumps(
            {
                "status": "SECURITY_AND_DEPLOYMENT_HARDENING_COMPLETE_MAINNET_LOCKED",
                "security_and_deployment_hardening_complete": True,
                "mainnet_live_exchange_write": "LOCKED",
                "real_mainnet_order_requests_sent": 0,
                "installer_network_access": "NONE",
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "acceptance.json"
    payload = build_acceptance_report(output=output, r37_report=report, project_root=project)
    assert payload["status"] == "PRODUCTION_RUNTIME_CLOSED_LOOP_COMPLETE_MAINNET_LOCKED"
    assert payload["required_failures"] == []
    assert payload["real_mainnet_order_requests_sent"] == 0
    assert output.is_file()
