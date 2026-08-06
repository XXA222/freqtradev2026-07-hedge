import json
from pathlib import Path

from freqtrade.hedge.r561.config import (
    validate_docker_dryrun_config,
    validate_r561_config,
)


def _template() -> dict:
    root = Path(__file__).resolve().parents[3]
    return json.loads(
        (root / "config_examples/config_hedge_r55_dryrun_ui.template.json").read_text(
            encoding="utf-8"
        )
    )


def test_template_closes_upstream_proxy_risk_and_safety_gaps() -> None:
    config = _template()
    report = validate_docker_dryrun_config(config)
    assert report.ready, report.errors
    assert validate_r561_config(config) == ()
    assert config["telegram"] == {"enabled": False, "token": "", "chat_id": ""}
    assert config["exchange"]["ccxt_config"]["requests_trust_env"] is True
    assert config["exchange"]["ccxt_async_config"]["aiohttp_trust_env"] is True
    assert config["position_adjustment_enable"] is False


def test_live_credentials_or_wrong_position_adjustment_owner_fail_closed() -> None:
    config = _template()
    config["exchange"]["key"] = "secret"
    config["hedge"]["r561"]["position_adjustment_owner"] = "freqtrade"
    report = validate_docker_dryrun_config(config)
    assert not report.ready
    assert "R561_POSITION_ADJUSTMENT_OWNER_INVALID" in report.errors
    assert "CHECK_FAILED:empty_exchange_credentials" in report.errors


def test_template_passes_bundled_upstream_json_schema() -> None:
    from jsonschema import Draft4Validator

    root = Path(__file__).resolve().parents[3]
    config = _template()
    schema = json.loads((root / "build_helpers/schema.json").read_text(encoding="utf-8"))
    errors = list(Draft4Validator(schema).iter_errors(config))
    assert not errors, [error.message for error in errors]


def test_docker_template_uses_container_bind_with_loopback_publish_contract():
    import json
    from pathlib import Path

    from freqtrade.hedge.r561.config import validate_docker_dryrun_config

    project = Path(__file__).resolve().parents[3]
    config = json.loads(
        (project / "config_examples/config_hedge_r561_docker_dryrun.template.json").read_text(
            encoding="utf-8"
        )
    )
    report = validate_docker_dryrun_config(config)
    assert report.ready, report.errors
    assert config["api_server"]["listen_ip_address"] == "0.0.0.0"
    assert config["hedge"]["r561"]["api_exposure_mode"] == "docker_loopback_publish"


def test_bootstrap_creates_secure_idempotent_docker_config(tmp_path):
    import json
    from pathlib import Path

    from freqtrade.hedge.r561.bootstrap import bootstrap_docker_config

    project = Path(__file__).resolve().parents[3]
    template = project / "config_examples/config_hedge_r561_docker_dryrun.template.json"
    destination = tmp_path / "user_data" / "config.json"
    first = bootstrap_docker_config(template_path=template, destination=destination)
    assert first.ready and first.created and first.updated_placeholders
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["exchange"]["key"] == ""
    assert payload["exchange"]["secret"] == ""
    assert payload["api_server"]["password"] not in {"", "__GENERATE_PASSWORD__"}
    before = destination.read_bytes()
    second = bootstrap_docker_config(template_path=template, destination=destination)
    assert second.ready and not second.created and not second.updated_placeholders
    assert destination.read_bytes() == before


def test_schema_extension_declares_strict_r56_and_r561_objects() -> None:
    from freqtrade.hedge.config_schema_extension import extend_config_schema

    schema = {"type": "object", "properties": {"api_server": {"type": "object"}}}
    extend_config_schema(schema)
    hedge = schema["properties"]["hedge"]
    assert hedge["additionalProperties"] is False
    assert hedge["properties"]["r56"]["additionalProperties"] is False
    assert hedge["properties"]["r561"]["additionalProperties"] is False
    assert "drawdown_kill" in hedge["properties"]["r56"]["properties"]
    assert "position_adjustment_owner" in hedge["properties"]["r561"]["properties"]
