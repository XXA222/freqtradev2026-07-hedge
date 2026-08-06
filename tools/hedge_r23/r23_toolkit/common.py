from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    argv: Iterable[str],
    *,
    cwd: Path,
    timeout: int = 120,
    check: bool = False,
) -> CommandResult:
    args = tuple(str(item) for item in argv)
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        result = CommandResult(args, completed.returncode, completed.stdout, completed.stderr)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        result = CommandResult(args, 127, "", str(exc))
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n{result.stderr.strip()}"
        )
    return result


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def sanitize_remote_url(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    if "://" in text:
        parts = urlsplit(text)
        hostname = parts.hostname or ""
        if parts.port:
            hostname = f"{hostname}:{parts.port}"
        return urlunsplit((parts.scheme, hostname, parts.path, parts.query, ""))
    return re.sub(r"^[^@\s]+@", "", text)


def redact_proxy_url(value: str) -> str:
    parts = urlsplit(value)
    hostname = parts.hostname or ""
    if parts.port:
        hostname = f"{hostname}:{parts.port}"
    if parts.username or parts.password:
        hostname = f"***:***@{hostname}"
    return urlunsplit((parts.scheme, hostname, parts.path, parts.query, ""))
