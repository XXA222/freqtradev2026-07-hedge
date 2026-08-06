from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    command: tuple[str, ...]
    timeout: int


def _run(section: Section, *, cwd: Path, output_dir: Path) -> dict[str, object]:
    stdout_path = output_dir / f"{section.name}.stdout.txt"
    stderr_path = output_dir / f"{section.name}.stderr.txt"
    started = time.monotonic()
    timed_out = False
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.run(
                section.command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=section.timeout,
                check=False,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        [str(cwd)]
                        + [str(path) for path in sorted((cwd / "tools").glob("hedge_r*")) if path.is_dir()]
                    ),
                },
            )
        exit_code = int(process.returncode)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = 124
        stderr_path.write_text("diagnostic section timed out\n", encoding="utf-8")
    except Exception as exc:
        exit_code = 125
        stderr_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
    return {
        "name": section.name,
        "command": list(section.command),
        "exit_code": exit_code,
        "passed": exit_code == 0,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }


def _security_scan(root: Path, output: Path) -> int:
    patterns = {
        "MAINNET_BYPASS": re.compile(r"mainnet_writes_enabled\s*[=:]\s*True"),
        "MARKET_ENABLE": re.compile(r"allow_market_orders\s*[=:]\s*True"),
        "REDIRECT_ENABLE": re.compile(r"allow_redirects\s*=\s*True|follow_redirects\s*=\s*True"),
        "SECRET_LITERAL": re.compile(r"api_secret\s*=\s*['\"][^'\"]{8,}['\"]"),
    }
    findings: list[dict[str, object]] = []
    roots = [root / "freqtrade/hedge", root / "tools/hedge_r40", root / "tools/hedge_r50"]
    for source_root in roots:
        if not source_root.exists():
            continue
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for name, pattern in patterns.items():
                for match in pattern.finditer(text):
                    findings.append({"rule": name, "path": str(path.relative_to(root)), "offset": match.start()})
    output.write_text(json.dumps({"findings": findings}, indent=2, sort_keys=True), encoding="utf-8")
    return 0 if not findings else 1


def _zip_directory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--upload-zip", type=Path, required=True)
    parser.add_argument("--r381-report", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.project_root.resolve(strict=True)
    out = args.output_dir.resolve(strict=False)
    out.mkdir(parents=True, exist_ok=True)
    python = root / "freqtrade-hedge/Scripts/python.exe"
    executable = str(python if python.is_file() else Path(sys.executable))
    r4_report = out / "R4-FULL-COMPLETION-READINESS.json"
    r5_report = out / "R5-LIMITED-MAINNET-READINESS.json"
    sections = [
        Section("01_pip_check", (executable, "-m", "pip", "check"), 180),
        Section("02_compileall", (executable, "-m", "compileall", "-q", "freqtrade/hedge", "tools/hedge_r40", "tools/hedge_r50"), 300),
        Section("03_r4_tests", (executable, "-m", "pytest", "-q", "tools/hedge_r40/tests"), 900),
        Section("04_r5_tests", (executable, "-m", "pytest", "-q", "tools/hedge_r50/tests"), 900),
        Section("05_r4_acceptance", (executable, "-m", "tools.hedge_r40.r40_toolkit", "--output", str(r4_report), "--r381-report", str(args.r381_report)), 300),
        Section("06_r5_acceptance", (executable, "-m", "tools.hedge_r50.r50_toolkit", "--output", str(r5_report), "--r4-report", str(r4_report), "--project-root", str(root)), 300),
        Section("07_execution_compatibility", (executable, "-m", "pytest", "-q", "tools/hedge_r30/tests", "tools/hedge_r31/tests", "tools/hedge_r32/tests", "tools/hedge_r33/tests", "tools/hedge_r34/tests", "tools/hedge_r35/tests", "tools/hedge_r36/tests", "tools/hedge_r37/tests", "tools/hedge_r38/tests"), 1800),
        Section("08_full_hedge_regression", (executable, "-m", "pytest", "-q", "tests/hedge"), 3600),
    ]
    results = [_run(section, cwd=root, output_dir=out) for section in sections]
    security_path = out / "09_security_scan.json"
    security_code = _security_scan(root, security_path)
    results.append({
        "name": "09_security_scan",
        "command": ["internal-security-scan"],
        "exit_code": security_code,
        "passed": security_code == 0,
        "timed_out": False,
        "duration_seconds": 0,
        "stdout": str(security_path),
        "stderr": "",
    })
    failed = [str(item["name"]) for item in results if not item["passed"]]
    summary = {
        "schema_version": 1,
        "toolkit_version": "5.0.0",
        "status": "R4_R5_FULL_DIAGNOSTICS_PASS" if not failed else "R4_R5_FULL_DIAGNOSTICS_HAS_FAILURES",
        "all_sections_attempted": True,
        "failed_sections": failed,
        "sections": results,
        "installer_exchange_access": "NONE",
        "real_mainnet_order_requests_sent_by_installer": 0,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    (out / "R4-R5-DIAGNOSTIC-SUMMARY.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8"
    )
    lines = [summary["status"], "", "All sections attempted: True"]
    for item in results:
        lines.append(f"{item['name']}: {'PASS' if item['passed'] else 'FAIL'} (exit={item['exit_code']})")
    if failed:
        lines.extend(["", "Failed sections:", *[f"- {name}" for name in failed]])
    (out / "R4-R5-DIAGNOSTIC-SUMMARY.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _zip_directory(out, args.upload_zip)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=True))
    print(f"Diagnostic upload ZIP: {args.upload_zip}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
