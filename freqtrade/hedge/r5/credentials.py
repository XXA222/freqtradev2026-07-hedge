from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from pathlib import Path

from freqtrade.hedge.execution.binance_usdm_adapter import BinanceExecutionCredentials

MAINNET_CREDENTIAL_MARKER = "BINANCE_USDM_MAINNET_R5"
R24_LEGACY_MISSING_ACCOUNT_ALIAS = "unknown"


@dataclass(frozen=True, slots=True)
class R5MainnetCredentials:
    api_key: str
    api_secret: str
    account_fingerprint: str
    source_format: str = "R5_MARKED"

    def execution_credentials(self) -> BinanceExecutionCredentials:
        return BinanceExecutionCredentials(self.api_key, self.api_secret)

    @property
    def account_id(self) -> str:
        return f"binance-usdm:{self.account_fingerprint}"

    def r24_account_fingerprint(self, account_alias: str | None) -> str:
        """Reproduce the legacy R2.4 fingerprint contract exactly.

        R2.4.1 read ``/fapi/v3/account`` and substituted the literal ``unknown``
        when Binance omitted ``accountAlias``.  The field is not guaranteed to
        be present, so R5 must preserve that historical fallback in order to
        bind new evidence to already accepted R2.4/R2.5 reports.
        """
        alias = str(account_alias or "").strip() or R24_LEGACY_MISSING_ACCOUNT_ALIAS
        material = f"r24-account:{alias}:{self.api_key}".encode("utf-8")
        return hmac.new(
            self.api_secret.encode("utf-8"), material, hashlib.sha256
        ).hexdigest()[:24]

    def r24_account_fingerprint_candidates(
        self, account_alias: str | None
    ) -> tuple[tuple[str, str], ...]:
        """Return ordered legacy-compatible evidence candidates.

        When an alias is present we validate both the live alias form and the
        R2.4 missing-alias fallback.  This covers schema drift without weakening
        the credential binding, because every candidate is still HMAC-bound to
        the current API key and secret.
        """
        alias = str(account_alias or "").strip()
        candidates: list[tuple[str, str]] = []
        if alias:
            candidates.append(("LIVE_ACCOUNT_ALIAS", self.r24_account_fingerprint(alias)))
        candidates.append((
            "R24_LEGACY_MISSING_ACCOUNT_ALIAS",
            self.r24_account_fingerprint(R24_LEGACY_MISSING_ACCOUNT_ALIAS),
        ))
        deduplicated: list[tuple[str, str]] = []
        observed: set[str] = set()
        for mode, fingerprint in candidates:
            if fingerprint not in observed:
                observed.add(fingerprint)
                deduplicated.append((mode, fingerprint))
        return tuple(deduplicated)

    def __repr__(self) -> str:
        return (
            "R5MainnetCredentials(api_key=<redacted>, api_secret=<redacted>, "
            f"source_format={self.source_format!r})"
        )


def load_mainnet_credentials(
    path: str | Path,
    *,
    allow_legacy_readonly: bool = False,
) -> R5MainnetCredentials:
    resolved = Path(path).expanduser().resolve(strict=True)
    lines = [item.strip() for item in resolved.read_text(encoding="utf-8-sig").splitlines()]
    lines = [item for item in lines if item]
    source_format = "R5_MARKED"
    if len(lines) == 2 and allow_legacy_readonly:
        key, secret = lines
        source_format = "LEGACY_READONLY"
    elif len(lines) == 3:
        key, secret, marker = lines
        if marker != MAINNET_CREDENTIAL_MARKER:
            raise ValueError("R5 mainnet credential marker is missing or invalid")
    else:
        expected = "two legacy lines for MAINNET_READONLY or three R5-marked lines"
        raise ValueError(f"R5 mainnet credential file must contain {expected}")
    if not key or not secret or len(key) > 512 or len(secret) > 512:
        raise ValueError("R5 mainnet credentials are invalid")
    fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return R5MainnetCredentials(key, secret, fingerprint, source_format)


@dataclass(frozen=True, slots=True)
class R5CredentialFileSecurity:
    path: str
    platform: str
    secure: bool
    owner: str | None
    reason_codes: tuple[str, ...]


def inspect_credential_file_security(path: str | Path) -> R5CredentialFileSecurity:
    """Inspect credential owner/permissions without reading or returning secret data."""
    import getpass
    import os
    import stat
    import subprocess

    resolved = Path(path).expanduser().resolve(strict=True)
    reasons: list[str] = []
    owner: str | None = None
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["icacls", str(resolved)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            reasons.append("ACL_INSPECTION_FAILED")
        else:
            output = (result.stdout or "") + "\n" + (result.stderr or "")
            normalized = output.upper()
            if result.returncode != 0:
                reasons.append("ACL_INSPECTION_FAILED")
            broad = ("EVERYONE", "BUILTIN\\USERS", "AUTHENTICATED USERS")
            dangerous = ("(F)", "(M)", "(W)")
            for line in normalized.splitlines():
                if any(principal in line for principal in broad) and any(
                    permission in line for permission in dangerous
                ):
                    reasons.append("BROAD_WRITE_ACL")
                    break
            owner = getpass.getuser()
    else:
        details = resolved.stat()
        owner = str(details.st_uid)
        if details.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            reasons.append("GROUP_OR_OTHER_ACCESS")
        try:
            if details.st_uid != os.getuid():
                reasons.append("OWNER_MISMATCH")
        except AttributeError:  # pragma: no cover - non-POSIX fallback
            pass
    return R5CredentialFileSecurity(
        str(resolved),
        os.name,
        not reasons,
        owner,
        tuple(dict.fromkeys(reasons)),
    )


def assert_credential_file_security(path: str | Path, *, for_exchange_write: bool) -> R5CredentialFileSecurity:
    report = inspect_credential_file_security(path)
    if for_exchange_write and not report.secure:
        raise PermissionError(
            "R5 credential file security check failed: " + ",".join(report.reason_codes)
        )
    return report
