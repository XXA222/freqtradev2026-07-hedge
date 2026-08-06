from __future__ import annotations

from r33_toolkit.acceptance import _verify_api_contract, _verify_sql_replay
from r33_toolkit.acceptance import _build_service


def test_acceptance_sqlite_temp_file_is_released_before_cleanup():
    assert _verify_sql_replay() is True


def test_acceptance_testclient_is_closed_deterministically():
    assert _verify_api_contract(_build_service()) is True
