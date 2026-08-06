from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from freqtrade.hedge.exchange.base import stable_fingerprint, to_primitive


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                to_primitive(dict(payload)),
                stream,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def append_jsonl(path: str | Path, payload: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        to_primitive(dict(payload)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with target.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def with_hash(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["report_hash"] = stable_fingerprint(result)
    return result


def scan_artifacts(paths: list[Path], secrets: tuple[str, str]) -> dict[str, Any]:
    failures: dict[str, list[str]] = {}
    scanned: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        data = path.read_bytes()
        found: list[str] = []
        if secrets[0] and secrets[0].encode("utf-8") in data:
            found.append("api_key")
        if secrets[1] and secrets[1].encode("utf-8") in data:
            found.append("api_secret")
        if found:
            failures[str(path)] = found
        scanned.append(str(path))
    if failures:
        raise RuntimeError(f"Credential material leaked into R2.5 artifacts: {failures}")
    return {"status": "PASS", "scanned_files": scanned}
