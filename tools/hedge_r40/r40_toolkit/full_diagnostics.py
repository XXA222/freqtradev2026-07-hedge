from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from freqtrade.hedge.r4.diagnostics import DiagnosticRunner
from tools.hedge_r40.r40_toolkit.forty_round_audit import run_forty_round_audit


_FORBIDDEN = (
    re.compile(r"mainnet_writes_enabled\s*[=:]\s*True"),
    re.compile(r"HEDGE_PRODUCTION_ARMED"),
    re.compile(r"allow_redirects\s*=\s*True"),
    re.compile(r"shell\s*=\s*True"),
    re.compile(r"https://fapi\.binance\.com"),
    re.compile(r"wss://fstream\.binance\.com"),
)


def _syntax_scan(root: Path) -> str:
    count = 0
    for path in sorted((root / "freqtrade" / "hedge" / "r4").rglob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += 1
    for path in sorted((root / "tools" / "hedge_r40").rglob("*.py")):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += 1
    return f"parsed={count}"


def _security_scan(root: Path) -> str:
    findings: list[str] = []
    paths = list((root / "freqtrade" / "hedge" / "r4").rglob("*.py"))
    paths += [
        path
        for path in (root / "tools" / "hedge_r40").rglob("*.py")
        if path.name not in {"full_diagnostics.py", "forty_round_audit.py"}
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for pattern in _FORBIDDEN:
            if pattern.search(text):
                findings.append(f"{path.relative_to(root)}:{pattern.pattern}")
    if findings:
        raise RuntimeError("; ".join(findings))
    return f"files={len(paths)} findings=0"


def _manifest_verify(package_root: Path) -> str:
    source_manifest = package_root / "SOURCE-MANIFEST.json"
    installed_manifest = package_root / "tools" / "hedge_r40" / "INSTALLED-PAYLOAD-MANIFEST.json"
    if source_manifest.is_file():
        manifest_path = source_manifest
        content_root = package_root
    elif installed_manifest.is_file():
        manifest_path = installed_manifest
        content_root = package_root
    else:
        raise FileNotFoundError("R4 source or installed payload manifest was not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures = []
    for item in manifest["files"]:
        relative = Path(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            failures.append(f"unsafe-path:{item['path']}")
            continue
        path = content_root / relative
        if not path.is_file():
            failures.append(f"missing:{item['path']}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            failures.append(f"hash:{item['path']}")
    if failures:
        raise RuntimeError("; ".join(failures))
    return f"files={len(manifest['files'])}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--r381-report", type=Path, required=True)
    parser.add_argument("--include-full-hedge", action="store_true", default=False)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    package = args.package_root.resolve()
    runner = DiagnosticRunner(args.output_dir)
    started = datetime.now(UTC)
    python = sys.executable

    runner.run_callable("01_manifest", lambda: _manifest_verify(package))
    runner.run_callable("02_syntax", lambda: _syntax_scan(root))
    runner.run_callable("03_security_scan", lambda: _security_scan(root))
    runner.run_command("04_pip_check", [python, "-m", "pip", "check"], cwd=root, timeout_seconds=300)
    runner.run_command(
        "05_r4_tests",
        [python, "-m", "pytest", "-q", "-o", "addopts=", "tools/hedge_r40/tests"],
        cwd=root,
        timeout_seconds=1800,
    )
    runner.run_callable(
        "06_forty_round_deep_audit",
        lambda: run_forty_round_audit(
            project_root=root,
            output_dir=args.output_dir / "forty-round-audit",
        )["status"],
    )
    runner.run_command(
        "07_r4_acceptance",
        [
            python,
            "-m",
            "tools.hedge_r40.r40_toolkit",
            "--output",
            str(args.output_dir / "R4-FULL-COMPLETION-READINESS.json"),
            "--r381-report",
            str(args.r381_report),
        ],
        cwd=root,
        timeout_seconds=600,
    )
    runner.run_command(
        "08_planning_simulation_compatibility",
        [
            python,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            "tests/hedge/planning",
            "tests/hedge/simulation",
            "tests/hedge/backtesting",
        ],
        cwd=root,
        timeout_seconds=1800,
    )
    runner.run_command(
        "09_runtime_execution_compatibility",
        [
            python,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            "tests/hedge/integration",
            "tests/hedge/execution",
            "tests/hedge/readiness",
            "tests/hedge/risk",
        ],
        cwd=root,
        timeout_seconds=1800,
    )
    if args.include_full_hedge:
        runner.run_command(
            "10_full_hedge_regression",
            [python, "-m", "pytest", "-q", "-o", "addopts=", "tests/hedge"],
            cwd=root,
            timeout_seconds=3600,
        )
    summary = runner.finish(started)
    print((args.output_dir / "R4-DIAGNOSTIC-SUMMARY.txt").read_text(encoding="utf-8"))
    return 0 if summary.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
