from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from freqtrade.hedge.deployment.backup import SQLiteBackupManager
from freqtrade.hedge.deployment.config import (
    DeploymentConfigError,
    DeploymentMode,
    HedgeDeploymentConfig,
)
from freqtrade.hedge.deployment.events import JsonlEventWriter
from freqtrade.hedge.deployment.readiness import DeploymentReadinessError, validate_r37_report
from freqtrade.hedge.deployment.single_instance import InstanceLockError, SingleInstanceLock
from freqtrade.hedge.deployment.state import RuntimePhase, RuntimeState, RuntimeStateStore
from freqtrade.hedge.deployment.supervisor import HedgeProcessSupervisor


def write_r37_report(path: Path, *, locked: bool = True) -> None:
    path.write_text(
        json.dumps(
            {
                "status": "SECURITY_AND_DEPLOYMENT_HARDENING_COMPLETE_MAINNET_LOCKED",
                "security_and_deployment_hardening_complete": True,
                "mainnet_live_exchange_write": "LOCKED" if locked else "ARMED",
                "real_mainnet_order_requests_sent": 0,
                "installer_network_access": "NONE",
            }
        ),
        encoding="utf-8",
    )


def make_config(tmp_path: Path, **overrides) -> HedgeDeploymentConfig:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    ft_config = project / "config.json"
    ft_config.write_text("{}", encoding="utf-8")
    python_executable = project / "python.exe"
    python_executable.write_text("", encoding="ascii")
    r37 = project / "r37.json"
    write_r37_report(r37)
    payload = {
        "project_root": str(project),
        "freqtrade_config": str(ft_config),
        "mode": "HEDGE_PRODUCTION_LOCKED",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "python_executable": str(python_executable),
        "r37_report": str(r37),
        "state_dir": "artifacts/runtime",
        "log_dir": "artifacts/runtime/logs",
        "backup_dir": "artifacts/runtime/backups",
        "heartbeat_interval_seconds": 1,
        "heartbeat_stale_seconds": 3,
        "graceful_shutdown_seconds": 2,
        "restart_policy": {
            "max_restarts": 2,
            "window_seconds": 30,
            "base_backoff_seconds": 0.1,
            "max_backoff_seconds": 0.2,
        },
    }
    payload.update(overrides)
    return HedgeDeploymentConfig.from_mapping(payload)


def test_config_accepts_only_locked_paper_or_testnet_validation(tmp_path):
    config = make_config(tmp_path)
    assert config.mode is DeploymentMode.PRODUCTION_LOCKED
    assert config.child_command()[1:4] == ("-m", "freqtrade", "trade")

    with pytest.raises(DeploymentConfigError):
        make_config(tmp_path, mode="HEDGE_PRODUCTION_ARMED")


def test_config_rejects_unknown_keys_and_live_flags(tmp_path):
    with pytest.raises(DeploymentConfigError, match="unknown"):
        make_config(tmp_path, surprise=True)
    with pytest.raises(DeploymentConfigError, match="forbidden"):
        make_config(tmp_path, additional_args=["--enable-live-trading"])


def test_config_rejects_path_escape_and_other_symbols(tmp_path):
    with pytest.raises(DeploymentConfigError, match="state_dir"):
        make_config(tmp_path, state_dir=str(tmp_path / "outside"))
    with pytest.raises(DeploymentConfigError, match="BTCUSDT"):
        make_config(tmp_path, symbols=["DOGEUSDT"])


def test_config_auto_discovers_project_python_and_latest_r37_report(tmp_path):
    project = tmp_path / "project"
    python = project / "freqtrade-hedge" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="ascii")
    ft_config = project / "config.json"
    ft_config.write_text("{}", encoding="utf-8")
    report_dir = project / "artifacts" / "hedge_r37_security_hardening" / "latest"
    report_dir.mkdir(parents=True)
    report = report_dir / "R3.7-SECURITY-DEPLOYMENT-HARDENING-READINESS.json"
    write_r37_report(report)
    config = HedgeDeploymentConfig.from_mapping(
        {
            "project_root": str(project),
            "freqtrade_config": str(ft_config),
            "mode": "HEDGE_PRODUCTION_LOCKED",
            "symbols": ["BTCUSDT"],
            "python_executable": "AUTO",
            "r37_report": "AUTO",
        }
    )
    assert config.python_executable == python.resolve()
    assert config.r37_report == report.resolve()


def test_readiness_requires_locked_r37(tmp_path):
    config = make_config(tmp_path)
    readiness = validate_r37_report(config)
    assert readiness.status == "READY_LIVE_LOCKED"
    write_r37_report(config.r37_report, locked=False)
    with pytest.raises(DeploymentReadinessError):
        validate_r37_report(config)


def test_single_instance_lock_blocks_second_owner(tmp_path):
    first = SingleInstanceLock(tmp_path / "runtime.lock")
    second = SingleInstanceLock(tmp_path / "runtime.lock")
    with first:
        assert first.identity is not None
        with pytest.raises(InstanceLockError):
            second.acquire()
    with second:
        assert second.identity is not None


def test_runtime_state_store_is_atomic_and_roundtrips(tmp_path):
    store = RuntimeStateStore(tmp_path / "state.json")
    state = RuntimeState(
        phase=RuntimePhase.RUNNING,
        supervisor_pid=10,
        instance_token="abc",
        child_pid=20,
        restart_count=1,
        started_at_utc="2026-08-03T00:00:00+00:00",
        updated_at_utc="2026-08-03T00:00:00+00:00",
        heartbeat_at_utc="2026-08-03T00:00:00+00:00",
    )
    store.write(state)
    assert store.read() == state
    assert not list(tmp_path.glob("*.tmp"))


def test_sqlite_backup_is_consistent_and_releases_files(tmp_path):
    source = tmp_path / "live.sqlite"
    connection = sqlite3.connect(source)
    try:
        connection.execute("create table evidence(id integer primary key, value text)")
        connection.execute("insert into evidence(value) values ('ok')")
        connection.commit()
    finally:
        connection.close()
    evidence = SQLiteBackupManager(tmp_path / "backups").create(source, label="prestart")
    backup = Path(evidence.backup)
    assert evidence.integrity_check == "ok"
    check = sqlite3.connect(backup)
    try:
        assert check.execute("select value from evidence").fetchone()[0] == "ok"
    finally:
        check.close()
    backup.unlink()
    source.unlink()


def test_event_writer_redacts_and_rotates(tmp_path):
    path = tmp_path / "events.jsonl"
    writer = JsonlEventWriter(path, max_bytes=1024)
    writer.write("TEST", api_key="secret-key", nested={"token": "secret-token", "safe": 1})
    payload = json.loads(path.read_text(encoding="ascii").splitlines()[0])
    assert payload["api_key"] == "[REDACTED]"
    assert payload["nested"]["token"] == "[REDACTED]"
    assert payload["nested"]["safe"] == 1
    for index in range(80):
        writer.write("FILL", index=index, text="x" * 80)
    assert path.with_suffix(".jsonl.1").is_file()


class FakeChild:
    def __init__(self, pid: int, polls: list[int | None]) -> None:
        self.pid = pid
        self.polls = polls
        self.terminated = False
        self.killed = False
        self.exit_code = polls[-1] if polls and polls[-1] is not None else 0

    def poll(self):
        if self.polls:
            value = self.polls.pop(0)
            if value is not None:
                self.exit_code = value
            return value
        return self.exit_code

    def wait(self, timeout=None):
        return int(self.exit_code)

    def terminate(self):
        self.terminated = True
        self.exit_code = 0
        self.polls = [0]

    def kill(self):
        self.killed = True
        self.exit_code = -9
        self.polls = [-9]


class FakeFactory:
    def __init__(self, children):
        self.children = list(children)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.children.pop(0)


def test_supervisor_clean_exit_uses_no_shell_and_writes_state(tmp_path, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "must-not-inherit")
    config = make_config(tmp_path)
    factory = FakeFactory([FakeChild(101, [None, 0])])
    supervisor = HedgeProcessSupervisor(config, process_factory=factory, sleep=lambda _: None)
    result = supervisor.run()
    assert result.phase is RuntimePhase.STOPPED
    assert result.exit_code == 0
    assert result.restart_count == 0
    command, kwargs = factory.calls[0]
    assert kwargs["shell"] is False
    assert command == list(config.child_command())
    assert kwargs["env"]["FREQTRADE_HEDGE_DEPLOYMENT_MODE"] == "HEDGE_PRODUCTION_LOCKED"
    assert "FREQTRADE__EXCHANGE__SECRET" not in kwargs["env"]
    state = RuntimeStateStore(config.state_dir / "runtime-state.json").read()
    assert state is not None and state.phase is RuntimePhase.STOPPED


def test_supervisor_bounded_restart_then_success(tmp_path):
    config = make_config(tmp_path)
    factory = FakeFactory([FakeChild(101, [2]), FakeChild(102, [0])])
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    supervisor = HedgeProcessSupervisor(
        config,
        process_factory=factory,
        sleep=lambda _: None,
        monotonic=lambda: next(clock),
    )
    result = supervisor.run()
    assert result.phase is RuntimePhase.STOPPED
    assert result.restart_count == 1
    assert len(factory.calls) == 2


def test_supervisor_exhausts_restart_budget(tmp_path):
    config = make_config(tmp_path, restart_policy={
        "max_restarts": 1,
        "window_seconds": 30,
        "base_backoff_seconds": 0.1,
        "max_backoff_seconds": 0.2,
    })
    factory = FakeFactory([FakeChild(101, [2]), FakeChild(102, [3])])
    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4])
    result = HedgeProcessSupervisor(
        config,
        process_factory=factory,
        sleep=lambda _: None,
        monotonic=lambda: next(ticks),
    ).run()
    assert result.phase is RuntimePhase.FAILED
    assert result.restart_count == 1
    state = RuntimeStateStore(config.state_dir / "runtime-state.json").read()
    assert state is not None and state.last_error == "RESTART_BUDGET_EXHAUSTED"


def test_stop_request_gracefully_terminates_managed_child(tmp_path):
    config = make_config(tmp_path)
    child = FakeChild(101, [None, None, None])
    factory = FakeFactory([child])
    holder = {}

    def sleep(_):
        holder["supervisor"].request_stop()

    supervisor = HedgeProcessSupervisor(config, process_factory=factory, sleep=sleep)
    holder["supervisor"] = supervisor
    result = supervisor.run()
    assert result.stopped_by_request is True
    assert child.terminated is True
    assert child.killed is False


def test_database_backup_occurs_only_after_instance_lock(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    db = project / "runtime.sqlite"
    connection = sqlite3.connect(db)
    connection.execute("create table t(id integer)")
    connection.commit()
    connection.close()
    config = make_config(tmp_path, database_path=str(db))
    held = SingleInstanceLock(config.state_dir / "supervisor.lock")
    with held:
        with pytest.raises(InstanceLockError):
            HedgeProcessSupervisor(
                config,
                process_factory=FakeFactory([FakeChild(1, [0])]),
                sleep=lambda _: None,
            ).run()
    assert not list(config.backup_dir.glob("*.sqlite"))


def test_single_instance_lock_normalizes_preopen_permission_denial(tmp_path, monkeypatch):
    lock = SingleInstanceLock(tmp_path / "runtime.lock")

    def denied():
        raise PermissionError(13, "simulated Windows sharing violation")

    monkeypatch.setattr(lock, "_open_handle", denied)
    with pytest.raises(InstanceLockError, match="another Hedge supervisor"):
        lock.acquire()


def test_single_instance_lock_acquires_before_identity_write(tmp_path, monkeypatch):
    calls: list[str] = []

    class FakeHandle:
        def seek(self, *_args):
            calls.append("seek")

        def write(self, _data):
            calls.append("write")
            return 1

        def truncate(self):
            calls.append("truncate")

        def flush(self):
            calls.append("flush")

        def fileno(self):
            return 1

        def close(self):
            calls.append("close")

    lock = SingleInstanceLock(tmp_path / "runtime.lock")
    fake = FakeHandle()
    monkeypatch.setattr(lock, "_open_handle", lambda: fake)
    monkeypatch.setattr(lock, "_lock_handle", lambda handle: calls.append("lock"))
    monkeypatch.setattr(os, "fsync", lambda _fd: calls.append("fsync"))

    lock.acquire()
    try:
        assert calls.index("lock") < calls.index("write")
    finally:
        monkeypatch.setattr(lock, "_unlock_handle", lambda handle: calls.append("unlock"))
        lock.release()
