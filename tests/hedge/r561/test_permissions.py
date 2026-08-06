from pathlib import Path

from freqtrade.hedge.r561.permissions import probe_directory


def test_permission_probe_checks_write_fsync_read_and_delete(tmp_path: Path) -> None:
    report = probe_directory(tmp_path / "user_data")
    assert report.ready
    assert report.write_ok and report.fsync_ok and report.read_ok and report.delete_ok
    assert not list((tmp_path / "user_data").glob(".hedge-write-probe-*"))
