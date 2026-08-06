from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from freqtrade.hedge.exchange import binance_normalizer


_PATCH_MARKER = "_r253_managed_symbol_config_scope_compat_installed"
_ORIGINAL_ATTR = "_r253_original_normalize_symbol_configuration_row"


def install_managed_symbol_config_scope_compat() -> dict[str, Any]:
    """Skip unrelated symbolConfig rows before strict symbol normalization.

    Binance may return account-level configuration rows for instruments outside
    the runtime managed-symbol set. Those rows must not be allowed to block the
    strict configuration validation of the managed symbols. Managed rows still
    use the original normalizer without any relaxation.
    """

    if bool(getattr(binance_normalizer, _PATCH_MARKER, False)):
        return {
            "installed": True,
            "already_installed": True,
            "strategy": "RAW_EXPECTED_MEMBERSHIP_BEFORE_NORMALIZATION",
        }

    original = binance_normalizer._normalize_symbol_configuration_row
    setattr(binance_normalizer, _ORIGINAL_ATTR, original)

    def managed_first(
        item: Mapping[str, Any],
        *,
        expected: set[str],
    ) -> tuple[str, str, int] | None:
        raw_symbol = str(item.get("symbol") or "").strip().upper()
        if raw_symbol not in expected:
            return None
        return original(item, expected=expected)

    binance_normalizer._normalize_symbol_configuration_row = managed_first
    setattr(binance_normalizer, _PATCH_MARKER, True)
    return {
        "installed": True,
        "already_installed": False,
        "strategy": "RAW_EXPECTED_MEMBERSHIP_BEFORE_NORMALIZATION",
    }
