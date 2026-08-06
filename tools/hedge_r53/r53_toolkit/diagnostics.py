from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import TOOLKIT_VERSION


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    temporary.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _toolkit_pythonpath_entries(root: Path) -> tuple[str, ...]:
    tools_root = root / "tools"
    entries: list[str] = [str(root), str(tools_root)]
    if tools_root.is_dir():
        for candidate in sorted(tools_root.glob("hedge_r*"), key=lambda item: item.name):
            if not candidate.is_dir():
                continue
            if any(child.is_dir() and child.name.endswith("_toolkit") for child in candidate.iterdir()):
                entries.append(str(candidate))
    return tuple(dict.fromkeys(entries))


def _run_section(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    output_dir: Path,
    timeout_seconds: int,
    env: Mapping[str, str],
) -> dict[str, Any]:
    stdout_path = output_dir / f"{name}.stdout.txt"
    stderr_path = output_dir / f"{name}.stderr.txt"
    started = time.monotonic()
    exit_code = 125
    timed_out = False
    launch_error: str | None = None
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=dict(env),
                stdout=stdout_file,
                stderr=stderr_file,
                stdin=subprocess.DEVNULL,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
                    else 0
                ),
            )
            try:
                exit_code = int(process.wait(timeout=timeout_seconds))
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                exit_code = 124
    except Exception as exc:
        launch_error = f"{type(exc).__name__}:{exc}"
        stderr_path.write_text(launch_error + "\n", encoding="utf-8")
    return {
        "name": name,
        "command": command,
        "exit_code": exit_code,
        "passed": exit_code == 0 and not timed_out and launch_error is None,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }


def _security_scan(project_root: Path, output_dir: Path) -> dict[str, Any]:
    targets = [
        project_root / "freqtrade/hedge/r5",
        project_root / "freqtrade/hedge/execution",
        project_root / "freqtrade/hedge/exchange/shared_rate_limit.py",
        project_root / "tools/hedge_r53",
        project_root / "scripts/Install-Freqtrade-Hedge-R5.3-AuthoritativeExecution.ps1",
        project_root / "scripts/Run-Freqtrade-Hedge-R5.3-MainnetProtectedAudit.ps1",
    ]
    findings: list[dict[str, Any]] = []
    scanned = 0
    # Match actual secret material, not scanner rule literals.  Building the PEM
    # pattern from fragments prevents this source file from self-triggering.
    pem_pattern = re.compile(
        "-" * 5 + r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY" + "-" * 5
    )
    assignment_patterns = (
        re.compile(r"(?im)^\s*(?:api_secret|api_key)\s*=\s*[\"'][^\"']{8,}[\"']"),
        re.compile(r"(?im)[\"']X-MBX-APIKEY[\"']\s*:\s*[\"'][^\"']{8,}[\"']"),
    )
    for target in targets:
        files: Iterable[Path] = target.rglob("*") if target.is_dir() else (target,)
        for path in files:
            if not path.is_file() or path.suffix.lower() not in {".py", ".ps1", ".json", ".md"}:
                continue
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            if pem_pattern.search(text):
                findings.append({"path": str(path), "reason": "POSSIBLE_EMBEDDED_PRIVATE_KEY"})
            for pattern in assignment_patterns:
                if pattern.search(text):
                    findings.append({"path": str(path), "reason": "POSSIBLE_EMBEDDED_API_CREDENTIAL"})
    protected_files = [
        project_root / "tools/hedge_r53/r53_toolkit/__main__.py",
        project_root / "scripts/Run-Freqtrade-Hedge-R5.3-MainnetProtectedAudit.ps1",
    ]
    for path in protected_files:
        if not path.is_file():
            findings.append({"path": str(path), "reason": "MISSING_PROTECTED_AUDIT_FILE"})
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in ("/fapi/v1/order", "/fapi/v1/order/test", "/fapi/v1/batchOrders"):
            if token in text:
                findings.append({"path": str(path), "reason": f"PROTECTED_AUDIT_ORDER_ENDPOINT:{token}"})
    emergency = project_root / "freqtrade/hedge/r5/emergency.py"
    runtime = project_root / "freqtrade/hedge/r5/authoritative_runtime.py"
    if not emergency.is_file() or "operator_sha256" not in emergency.read_text(encoding="utf-8") or "risk_sha256" not in emergency.read_text(encoding="utf-8"):
        findings.append({"path": str(emergency), "reason": "EMERGENCY_DUAL_AUTHORITY_MISSING"})
    if not runtime.is_file() or "execute_emergency_reduce" not in runtime.read_text(encoding="utf-8"):
        findings.append({"path": str(runtime), "reason": "EMERGENCY_NOT_IN_AUTHORITATIVE_RUNTIME"})
    payload = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "status": "PASS" if not findings else "FAIL",
        "scanned_files": scanned,
        "finding_count": len(findings),
        "findings": findings,
        "installer_exchange_access": "NONE",
        "real_mainnet_order_requests_sent": 0,
    }
    path = output_dir / "12_security_scan.json"
    _atomic_json(path, payload)
    return {
        "name": "12_security_scan",
        "command": ["internal-security-scan"],
        "exit_code": 0 if not findings else 2,
        "passed": not findings,
        "timed_out": False,
        "launch_error": None,
        "duration_seconds": 0,
        "stdout": str(path),
        "stderr": "",
    }


def _audit_matrix_check(project_root: Path, output_dir: Path) -> dict[str, Any]:
    path = project_root / "R5.3-AUDIT-REMEDIATION-MATRIX.md"
    findings: list[str] = []
    if not path.is_file():
        findings.append("MATRIX_MISSING")
        text = ""
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
    for prefix, count in (("M-P0-", 8), ("M-P1-", 20)):
        for index in range(1, count + 1):
            identifier = f"{prefix}{index:02d}"
            if identifier not in text:
                findings.append(f"MISSING:{identifier}")
    if "EVIDENCE PENDING" not in text:
        findings.append("MISSING_24_72_HOUR_EVIDENCE_DISCLOSURE")
    payload = {
        "schema_version": 1,
        "status": "PASS" if not findings else "FAIL",
        "findings": findings,
        "matrix": str(path),
    }
    result = output_dir / "13_audit_matrix.json"
    _atomic_json(result, payload)
    return {
        "name": "13_audit_matrix",
        "command": ["internal-audit-matrix-check"],
        "exit_code": 0 if not findings else 2,
        "passed": not findings,
        "timed_out": False,
        "launch_error": None,
        "duration_seconds": 0,
        "stdout": str(result),
        "stderr": "",
    }


def _zip_directory(source: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())
    temporary.replace(destination)


def run(args: argparse.Namespace) -> int:
    root = Path(args.project_root).expanduser().resolve(strict=True)
    python = Path(args.python).expanduser().resolve(strict=True)
    output = Path(args.output_dir).expanduser().resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    toolkit_roots = _toolkit_pythonpath_entries(root)
    entries = list(toolkit_roots)
    if env.get("PYTHONPATH"):
        entries.extend(item for item in env["PYTHONPATH"].split(os.pathsep) if item)
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(entries))
    env["PYTHONUTF8"] = "1"
    py = str(python)
    specs = [
        ("01_pip_check", [py, "-m", "pip", "check"], 300),
        ("02_compileall", [py, "-m", "compileall", "-q", "freqtrade/hedge/r5", "freqtrade/hedge/r4", "freqtrade/hedge/execution", "freqtrade/hedge/exchange", "freqtrade/persistence", "freqtrade/rpc/api_server", "tools/hedge_r53"], 600),
        ("03_r53_tests", [py, "-m", "pytest", "-q", "tools/hedge_r53/tests"], 1200),
        ("04_r52_tests", [py, "-m", "pytest", "-q", "tools/hedge_r52/tests"], 900),
        ("05_r51_tests", [py, "-m", "pytest", "-q", "tools/hedge_r51/tests"], 900),
        ("06_r50_tests", [py, "-m", "pytest", "-q", "tools/hedge_r50/tests"], 900),
        ("07_r40_tests", [py, "-m", "pytest", "-q", "tools/hedge_r40/tests"], 900),
        ("08_execution_compatibility", [py, "-m", "pytest", "-q", "tools/hedge_r30/tests", "tools/hedge_r31/tests", "tools/hedge_r32/tests", "tools/hedge_r33/tests", "tools/hedge_r34/tests", "tools/hedge_r35/tests", "tools/hedge_r36/tests", "tools/hedge_r37/tests", "tools/hedge_r38/tests"], 1800),
        ("09_full_hedge_regression", [py, "-m", "pytest", "-q", "tests/hedge"], 2400),
        ("10_r53_offline_acceptance", [py, "-m", "tools.hedge_r53.r53_toolkit.offline_acceptance", "--project-root", str(root), "--output", str(output / "R5.3-AUTHORITATIVE-EXECUTION-READINESS.json")], 900),
        ("11_r52_offline_acceptance", [py, "-m", "tools.hedge_r52.r52_toolkit.offline_acceptance", "--project-root", str(root), "--output", str(output / "R5.2-PRODUCTION-STRATEGY-CORE-READINESS.json")], 900),
    ]
    sections = [
        _run_section(name=name, command=command, cwd=root, output_dir=output, timeout_seconds=timeout, env=env)
        for name, command, timeout in specs
    ]
    sections.append(_security_scan(root, output))
    sections.append(_audit_matrix_check(root, output))
    failed = [section["name"] for section in sections if not section["passed"]]
    summary: dict[str, Any] = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "status": "R5_3_FULL_DIAGNOSTICS_PASS" if not failed else "R5_3_FULL_DIAGNOSTICS_FAIL",
        "all_sections_attempted": len(sections) == 13,
        "failed_sections": failed,
        "sections": sections,
        "installer_exchange_access": "NONE",
        "real_mainnet_order_requests_sent_by_installer": 0,
        "mainnet_live_exchange_write": "LOCKED",
        "pythonpath_toolkit_roots": list(toolkit_roots),
        "unattended_24_72_hour_evidence": "PENDING_ELAPSED_OPERATIONAL_TIME",
    }
    install_manifest = os.environ.get("R53_INSTALL_MANIFEST")
    if install_manifest:
        summary["install_manifest"] = install_manifest
    canonical = json.dumps(summary, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    summary["canonical_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    _atomic_json(output / "R5.3-DIAGNOSTIC-SUMMARY.json", summary)
    lines = [
        f"status={summary['status']}",
        f"all_sections_attempted={summary['all_sections_attempted']}",
        f"failed_sections={','.join(failed) if failed else 'NONE'}",
        "installer_exchange_access=NONE",
        "real_mainnet_order_requests_sent_by_installer=0",
        "unattended_24_72_hour_evidence=PENDING_ELAPSED_OPERATIONAL_TIME",
        "",
    ]
    for section in sections:
        lines.append(
            f"{section['name']}: {'PASS' if section['passed'] else 'FAIL'} "
            f"exit={section['exit_code']} timeout={section['timed_out']} duration={section['duration_seconds']}s"
        )
    _atomic_text(output / "R5.3-DIAGNOSTIC-SUMMARY.txt", "\n".join(lines) + "\n")
    downloads = Path.home() / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    upload = downloads / f"Freqtrade_Hedge_R5.3_InstallDiagnosticUpload_{output.name}.zip"
    _zip_directory(output, upload)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=True))
    print(f"Diagnostic upload ZIP: {upload}")
    return 0 if not failed else 2


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="hedge-r53-diagnostics")
    value.add_argument("--project-root", required=True)
    value.add_argument("--python", required=True)
    value.add_argument("--output-dir", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
