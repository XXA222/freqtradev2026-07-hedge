"""Local two-line Binance credential loading without persistence or logging."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_RELATIVE_PATH_B64 = (
    "RGVza3RvcFzlkK/liqhmcmVxdHJhZGVcYXBpXGJpbmFuY2UgLSDlia/mnKwudHh0"
)


@dataclass(frozen=True, slots=True)
class LocalBinanceCredentials:
    api_key: str
    api_secret: str

    def __post_init__(self) -> None:
        key = str(self.api_key).strip()
        secret = str(self.api_secret).strip()
        if not key or not secret:
            raise ValueError("credential file contains an empty API key or secret")
        object.__setattr__(self, "api_key", key)
        object.__setattr__(self, "api_secret", secret)

    def __repr__(self) -> str:
        return "LocalBinanceCredentials(api_key=<redacted>, api_secret=<redacted>)"


def default_credential_path(*, user_profile: str | Path) -> Path:
    relative = base64.b64decode(_DEFAULT_RELATIVE_PATH_B64).decode("utf-8")
    return Path(user_profile) / Path(relative)


def load_local_binance_credentials(path: str | Path) -> LocalBinanceCredentials:
    candidate = Path(path)
    if not candidate.is_file():
        raise FileNotFoundError(f"credential file does not exist: {candidate}")
    lines = candidate.read_text(encoding="utf-8-sig").splitlines()
    if len(lines) < 2:
        raise ValueError("credential file must contain API key on line 1 and API secret on line 2")
    return LocalBinanceCredentials(lines[0], lines[1])
