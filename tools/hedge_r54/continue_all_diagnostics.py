from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from freqtrade.hedge.r54.canonical import sha256_file
from freqtrade.hedge.r54.runner import CommandSpec, run_continue_all


def build_specs(project_root: Path, python: Path, output: Path) -> tuple[CommandSpec, ...]:
    env = os.environ.copy()
    paths = [
        str(project_root),
        str(project_root / "tools"),
        str(project_root / "tools/hedge_r53"),
        str(project_root / "tools/hedge_r531"),
        str(project_root / "tools/hedge_r532"),
        str(project_root / "tools/hedge_r54"),
    ]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONUTF8"] = "1"
    common = {"cwd": str(project_root), "environment": env}
    return (
        CommandSpec(
            "01_compileall_r54",
            (
                str(python),
                "-m",
                "compileall",
                "-q",
                "freqtrade/hedge/r54",
                "tests/hedge/r54",
                "tools/hedge_r54",
            ),
            **common,
        ),
        CommandSpec(
            "02_r54_tests",
            (
                str(python),
                "-m",
                "pytest",
                "-q",
                "tests/hedge/r54",
                "--confcutdir=tests/hedge/r54",
                "-o",
                "addopts=",
            ),
            **common,
        ),
        CommandSpec(
            "03_r54_offline_acceptance",
            (
                str(python),
                "tools/hedge_r54/offline_acceptance.py",
                str(output / "R5.4-OFFLINE-ACCEPTANCE.json"),
            ),
            **common,
        ),
        CommandSpec(
            "04_r54_source_audit",
            (
                str(python),
                "tools/hedge_r54/source_audit.py",
                str(project_root),
                str(output / "R5.4-SOURCE-AUDIT.json"),
            ),
            **common,
        ),
        CommandSpec(
            "05_r532_tests",
            (str(python), "-m", "pytest", "-q", "tools/hedge_r532/tests"),
            **common,
        ),
        CommandSpec(
            "06_r532_offline_acceptance",
            (
                str(python),
                "-m",
                "tools.hedge_r532.r532_toolkit.offline_acceptance",
                "--project-root",
                str(project_root),
                "--output",
                str(output / "R5.3.2-RESOURCE-CLOSURE-READINESS.json"),
            ),
            **common,
        ),
        CommandSpec(
            "07_r53_full_diagnostics",
            (
                str(python),
                "-m",
                "tools.hedge_r53.r53_toolkit.diagnostics",
                "--project-root",
                str(project_root),
                "--python",
                str(python),
                "--output-dir",
                str(output / "R5.3-FULL-DIAGNOSTICS"),
            ),
            timeout_seconds=7200,
            **common,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--result-zip")
    args = parser.parse_args()

    project_root = Path(args.project_root).expanduser().resolve(strict=True)
    python = Path(args.python).expanduser().resolve(strict=True)
    output = Path(args.output_dir).expanduser().resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)

    summary = run_continue_all(
        build_specs(project_root, python, output),
        artifact_dir=output / "steps",
        summary_path=output / "R5.4-DIAGNOSTIC-SUMMARY.json",
    )
    digest = ["R5.4 CONTINUE-ALL DIAGNOSTICS", ""]
    for item in summary.results:
        digest.append(
            f"{item.name}: {'PASS' if item.passed else 'FAIL'} exit={item.exit_code}"
        )
        digest.append(f"  stdout=steps/{item.stdout_path}")
        digest.append(f"  stderr=steps/{item.stderr_path}")
    (output / "ALL-RESULTS.txt").write_text("\n".join(digest) + "\n", encoding="utf-8")

    result_zip = Path(args.result_zip) if args.result_zip else output.with_suffix(".zip")
    if result_zip.exists():
        result_zip.unlink()
    result_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(result_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output).as_posix())
    digest_value = sha256_file(result_zip)
    result_zip.with_suffix(result_zip.suffix + ".sha256.txt").write_text(
        f"{digest_value}  {result_zip.name}\n", encoding="ascii"
    )
    payload = {
        "status": "PASS" if summary.passed else "FAIL",
        "created_at": datetime.now(UTC).isoformat(),
        "result_zip": str(result_zip),
        "sha256": digest_value,
        "failed_names": list(summary.failed_names),
        "steps_attempted": len(summary.results),
        "exchange_access": "NONE",
        "real_order_requests": 0,
        "mainnet_writes": "LOCKED",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if summary.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
