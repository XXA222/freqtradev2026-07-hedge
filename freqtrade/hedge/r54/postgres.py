from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .evidence import create_envelope
from .models import ArtifactKind, CheckStatus, EvidenceEnvelope


_REQUIRED_CHECKS = (
    "fresh_migration",
    "idempotent_upgrade",
    "upgrade_downgrade_upgrade",
    "schema_parity",
    "constraint_parity",
    "concurrent_writer_fencing",
    "outbox_recovery",
    "backup_restore",
)


@dataclass(frozen=True, slots=True)
class PostgresMatrixResult:
    checks: Mapping[str, bool]
    pytest_passed: int
    pytest_failed: int
    server_version: str
    dsn_redacted: str

    def __post_init__(self) -> None:
        missing = [name for name in _REQUIRED_CHECKS if name not in self.checks]
        if missing:
            raise ValueError(f"PostgreSQL matrix missing checks: {', '.join(missing)}")
        if self.pytest_passed < 0 or self.pytest_failed < 0:
            raise ValueError("pytest counts must be nonnegative")
        lowered = self.dsn_redacted.lower()
        if "@" in self.dsn_redacted and "***" not in self.dsn_redacted:
            raise ValueError("dsn_redacted may not expose credentials")
        if "password=" in lowered and "password=***" not in lowered:
            raise ValueError("dsn_redacted may not expose a password")

    @property
    def passed(self) -> bool:
        return all(bool(self.checks[name]) for name in _REQUIRED_CHECKS) and self.pytest_failed == 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "checks": {name: bool(self.checks[name]) for name in _REQUIRED_CHECKS},
            "pytest_passed": self.pytest_passed,
            "pytest_failed": self.pytest_failed,
            "server_version": self.server_version,
            "dsn_redacted": self.dsn_redacted,
        }


def build_postgres_evidence(
    result: PostgresMatrixResult,
    *,
    account_id: str,
    producer: str = "r54-postgres-matrix",
) -> EvidenceEnvelope:
    return create_envelope(
        kind=ArtifactKind.POSTGRES_MATRIX,
        account_id=account_id,
        environment="LOCAL_POSTGRES",
        producer=producer,
        payload=result.to_payload(),
        status=CheckStatus.PASS if result.passed else CheckStatus.FAIL,
        ttl_seconds=30 * 24 * 3600,
        write_surface="NONE",
    )


def postgres_command_plan(python_executable: str) -> tuple[tuple[str, ...], ...]:
    tests = (
        "tests/hedge/migrations/test_hedge_migrations_postgresql.py",
        "tests/hedge/migrations/test_hedge_postgres_r20_authority.py",
        "tests/hedge/migrations/test_postgresql_restore_order_r20.py",
    )
    return tuple((python_executable, "-m", "pytest", "-q", test) for test in tests)
