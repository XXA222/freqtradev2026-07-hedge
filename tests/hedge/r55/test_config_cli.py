import json
from pathlib import Path
from freqtrade.hedge.r55.cli import validate_dryrun_config

def template():return json.loads(Path("config_examples/config_hedge_r55_dryrun_ui.template.json").read_text())
def test_template_only_needs_generated_secrets():
    c=template();errors=validate_dryrun_config(c)
    assert set(errors)=={"API_PASSWORD_WEAK","API_JWT_SECRET_KEY_WEAK","API_WS_TOKEN_WEAK"}
def test_generated_local_config_passes():
    c=template();c["api_server"].update(password="x"*24,jwt_secret_key="y"*40,ws_token="z"*24)
    assert validate_dryrun_config(c)==()
def test_remote_or_credentials_rejected():
    c=template();c["api_server"].update(password="x"*24,jwt_secret_key="y"*40,ws_token="z"*24,listen_ip_address="0.0.0.0");c["exchange"]["key"]="secret"
    errors=validate_dryrun_config(c);assert "API_MUST_BIND_LOOPBACK" in errors and "EXCHANGE_CREDENTIALS_MUST_BE_EMPTY" in errors
