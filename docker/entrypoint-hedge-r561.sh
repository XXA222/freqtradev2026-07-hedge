#!/bin/sh
set -eu

export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"
export NO_COLOR="${NO_COLOR:-1}"

CONFIG_PATH="${FREQTRADE_CONFIG_PATH:-/opt/freqtrade-hedge/user_data/config_hedge_r56_dryrun_operations.json}"
TEMPLATE_PATH="${FREQTRADE_CONFIG_TEMPLATE:-/opt/freqtrade-hedge/config_examples/config_hedge_r561_docker_dryrun.template.json}"

/opt/hedge-venv/bin/python - <<'PY'
import pathlib
import sys
import freqtrade
from freqtrade.hedge.r561.permissions import probe_directory

assert sys.version_info[:2] == (3, 12), sys.version
project = pathlib.Path('/opt/freqtrade-hedge').resolve()
module = pathlib.Path(freqtrade.__file__).resolve()
assert project in module.parents, (project, module)
report = probe_directory('/opt/freqtrade-hedge/user_data')
if not report.ready:
    raise SystemExit('USER_DATA_PERMISSION_GATE_FAILED:' + ','.join(report.reasons))
print('R5.6.1 ENTRYPOINT ENVIRONMENT: PASS')
PY

/opt/hedge-venv/bin/python -m freqtrade.hedge.r561 bootstrap-config \
    --template "$TEMPLATE_PATH" \
    --destination "$CONFIG_PATH"

/opt/hedge-venv/bin/python -m freqtrade.hedge.r561 validate-config \
    "$CONFIG_PATH" --upstream-schema

exec "$@"
