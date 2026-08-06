from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from freqtrade.hedge.r54.config_validation import validate_config_manifest


REQUIRED_FILES = (
    "R5.4-20-ROUND-DEVELOPMENT-REPORT.md",
    "R5.4-RELEASE-NOTES.md",
    "R5.4-SOURCE-MANIFEST.json",
    "R5.4-RELEASE-MANIFEST.json",
    "R5.4-TEST-REPORT.txt",
    "docs/hedge-r5.4-operational-evidence-and-promotion.md",
    "config_examples/R5.4-CONFIG-MANIFEST.json",
    "config_examples/config_hedge_r54_operational_evidence.example.json",
    "freqtrade/hedge/r54/__init__.py",
    "freqtrade/hedge/r54/cli.py",
    "tools/hedge_r54/offline_acceptance.py",
)


def run(project_root: Path) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}

    missing = [name for name in REQUIRED_FILES if not (project_root / name).is_file()]
    checks["required_files_present"] = not missing
    details["missing_files"] = missing

    syntax_errors: list[str] = []
    source_files = tuple(sorted((project_root / "freqtrade/hedge/r54").glob("*.py")))
    source_files += tuple(sorted((project_root / "tools/hedge_r54").glob("*.py")))
    source_files += tuple(sorted((project_root / "tests/hedge/r54").glob("*.py")))
    for path in source_files:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            syntax_errors.append(f"{path.relative_to(project_root)}:{exc.lineno}:{exc.msg}")
    checks["python_ast_clean"] = not syntax_errors
    details["syntax_errors"] = syntax_errors

    source_manifest = project_root / "R5.4-SOURCE-MANIFEST.json"
    manifest_cache_entries: list[str] = []
    if source_manifest.is_file():
        manifest_payload = json.loads(source_manifest.read_text(encoding="utf-8-sig"))
        for entry in manifest_payload.get("files", []):
            relative = str(entry.get("relative_path", ""))
            if relative.lower().endswith((".pyc", ".pyo")) or "__pycache__" in relative:
                manifest_cache_entries.append(relative)
    checks["source_manifest_cache_free"] = not manifest_cache_entries
    details["manifest_cache_entries"] = manifest_cache_entries

    forbidden_markers: list[str] = []
    for path in source_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"
        if private_key_marker in text:
            forbidden_markers.append(path.relative_to(project_root).as_posix())
    checks["no_embedded_private_keys"] = not forbidden_markers
    details["private_key_files"] = forbidden_markers

    config_manifest = project_root / "config_examples/R5.4-CONFIG-MANIFEST.json"
    if config_manifest.is_file():
        config_ok, config_details = validate_config_manifest(
            config_manifest, root=config_manifest.parent
        )
    else:
        config_ok, config_details = False, {"errors": ["missing_manifest"]}
    checks["config_manifest_valid"] = config_ok
    details["config_manifest"] = config_details

    pyproject = (project_root / "pyproject.toml").read_text(encoding="utf-8")
    checks["console_entrypoint_present"] = (
        'freqtrade-hedge-r54 = "freqtrade.hedge.r54.cli:main"' in pyproject
    )

    deployment = json.loads(
        (project_root / "config_examples/config_hedge_r54_operational_evidence.example.json")
        .read_text(encoding="utf-8")
    )
    checks["mainnet_default_locked"] = deployment.get("mainnet_writes_default") == "LOCKED"

    checks["twenty_round_report_complete"] = all(
        f"| {index:02d} |" in (project_root / REQUIRED_FILES[0]).read_text(encoding="utf-8")
        for index in range(1, 21)
    )

    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema_version": 1,
        "toolkit": "R5.4.0",
        "status": "PASS" if not failed else "FAIL",
        "check_count": len(checks),
        "passed_count": len(checks) - len(failed),
        "failed_count": len(failed),
        "failed_checks": failed,
        "checks": checks,
        "details": details,
        "exchange_access": "NONE",
        "real_order_requests": 0,
        "mainnet_writes": "LOCKED",
    }


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        print("usage: source_audit.py PROJECT_ROOT [OUTPUT_JSON]", file=sys.stderr)
        return 64
    project_root = Path(sys.argv[1]).expanduser().resolve(strict=True)
    result = run(project_root)
    if len(sys.argv) == 3:
        output = Path(sys.argv[2]).expanduser().resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
