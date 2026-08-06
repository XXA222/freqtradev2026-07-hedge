from __future__ import annotations

import json
import zipfile

from freqtrade.hedge.r54.backup import (
    create_backup_manifest,
    run_backup_restore_drill,
    verify_backup_manifest,
)
from freqtrade.hedge.r54.bundle import build_evidence_bundle, verify_evidence_bundle
from freqtrade.hedge.r54.models import CheckStatus


def test_backup_manifest_detects_change(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    manifest = create_backup_manifest(source)
    assert verify_backup_manifest(source, manifest) == (True, ())
    (source / "a.txt").write_text("changed", encoding="utf-8")
    passed, errors = verify_backup_manifest(source, manifest)
    assert not passed
    assert errors == ("size:a.txt",)


def test_backup_restore_drill_passes(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("a", encoding="utf-8")
    evidence = run_backup_restore_drill(source, account_id="acct")
    assert evidence.status is CheckStatus.PASS


def test_evidence_bundle_detects_tamper(tmp_path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    bundle = build_evidence_bundle(tmp_path / "bundle.zip", [evidence], account_id="acct")
    assert verify_evidence_bundle(bundle) == (True, ())
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(tampered, "w") as target:
        for name in source.namelist():
            data = b"tampered" if name == "evidence/evidence.json" else source.read(name)
            target.writestr(name, data)
    passed, errors = verify_evidence_bundle(tampered)
    assert not passed
    assert any(item.startswith(("size:", "hash:")) for item in errors)
