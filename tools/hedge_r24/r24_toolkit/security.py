from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    source: str

    def validate(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ValueError("Both Binance API key and secret are required")
        if len(self.api_key) < 16 or len(self.api_secret) < 16:
            raise ValueError("Binance credentials are unexpectedly short")


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Credential config must contain a JSON object: {path}")
    return value


def load_credentials(config_path: Path | None = None) -> Credentials:
    env_candidates = (
        ("BINANCE_API_KEY", "BINANCE_API_SECRET"),
        ("FREQTRADE__EXCHANGE__KEY", "FREQTRADE__EXCHANGE__SECRET"),
    )
    partial_sources: list[str] = []
    for key_name, secret_name in env_candidates:
        key = os.environ.get(key_name, "").strip()
        secret = os.environ.get(secret_name, "").strip()
        if key and secret:
            credentials = Credentials(key, secret, f"environment:{key_name}")
            credentials.validate()
            return credentials
        if key or secret:
            partial_sources.append(key_name)

    if config_path is not None:
        config = _read_json(config_path)
        exchange = config.get("exchange")
        if not isinstance(exchange, dict):
            raise ValueError("Credential config does not contain an exchange object")
        credentials = Credentials(
            str(exchange.get("key", "")).strip(),
            str(exchange.get("secret", "")).strip(),
            "config_file",
        )
        credentials.validate()
        return credentials

    suffix = f" Partial environment pair(s): {partial_sources}." if partial_sources else ""
    raise ValueError(
        "No credentials found. Use BINANCE_API_KEY/BINANCE_API_SECRET, "
        "FREQTRADE__EXCHANGE__KEY/FREQTRADE__EXCHANGE__SECRET, or --credential-config."
        + suffix
    )


def contains_secret_bytes(path: Path, secrets: list[str]) -> list[str]:
    data = path.read_bytes()
    found: list[str] = []
    for label, value in (("api_key", secrets[0]), ("api_secret", secrets[1])):
        if value and value.encode("utf-8") in data:
            found.append(label)
    return found
