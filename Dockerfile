# Freqtrade-Hedge R5.6.1 reproducible Python 3.12 runtime.
# Build with a locally verified base image to avoid unreliable registry mirrors:
#   docker build --pull=false --build-arg PYTHON_BASE_IMAGE=freqtrade-hedge-python-base:3.12 .
ARG PYTHON_BASE_IMAGE=python:3.12.13-slim-bookworm
FROM ${PYTHON_BASE_IMAGE} AS base

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    NO_COLOR=1 \
    FT_APP_ENV=docker \
    VIRTUAL_ENV=/opt/hedge-venv \
    PATH=/opt/hedge-venv/bin:$PATH

RUN python -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version" \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl git libffi-dev libgomp1 libssl-dev sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/hedge-venv \
    && /opt/hedge-venv/bin/python -m pip install --upgrade pip wheel setuptools

WORKDIR /opt/freqtrade-hedge

# Copy dependency metadata first so this layer remains cacheable.
COPY pyproject.toml requirements*.txt MANIFEST.in README.md LICENSE ./
COPY ft_client ./ft_client

# Copy only project source selected by .dockerignore.
COPY freqtrade ./freqtrade
COPY config_examples ./config_examples
COPY scripts ./scripts
COPY tools ./tools
COPY docker/entrypoint-hedge-r561.sh /usr/local/bin/entrypoint-hedge-r561

ARG INSTALL_DEVELOP=false
RUN chmod 0755 /usr/local/bin/entrypoint-hedge-r561 \
    && /opt/hedge-venv/bin/python -m pip install --no-cache-dir ./ft_client \
    && if [ "$INSTALL_DEVELOP" = "true" ]; then \
         /opt/hedge-venv/bin/python -m pip install --no-cache-dir '.[develop]'; \
       else \
         /opt/hedge-venv/bin/python -m pip install --no-cache-dir .; \
       fi \
    && /opt/hedge-venv/bin/python -m pip check \
    && /opt/hedge-venv/bin/python - <<'PY'
import pathlib
import sys
import freqtrade
assert sys.version_info[:2] == (3, 12), sys.version
root = pathlib.Path('/opt/freqtrade-hedge').resolve()
module = pathlib.Path(freqtrade.__file__).resolve()
assert root in module.parents, (root, module)
print('PYTHON_ABI_IMPORT_GATE: PASS')
PY

RUN groupadd --gid 1000 ftuser \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash ftuser \
    && mkdir -p /opt/freqtrade-hedge/user_data /opt/freqtrade-hedge/artifacts \
    && chown -R ftuser:ftuser /opt/freqtrade-hedge /opt/hedge-venv

USER ftuser
EXPOSE 8080
VOLUME ["/opt/freqtrade-hedge/user_data"]
ENTRYPOINT ["/usr/local/bin/entrypoint-hedge-r561"]
CMD ["freqtrade", "trade", "--config", "/opt/freqtrade-hedge/user_data/config_hedge_r56_dryrun_operations.json"]
