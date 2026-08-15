#!/usr/bin/env python3
"""Run focused CPU/adaptive regression tests without pytest/xdist dependency."""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
from pathlib import Path
import traceback
import unittest

MODULES = (
    "tests.hedge.performance.test_cpu_hotpath_optimization",
    "tests.hedge.performance.test_resource_governor",
    "tests.hedge.simulation.test_wallet_matcher",
    "tests.hedge.planning.test_planner",
    "tests.hedge.optimization.test_memory_opt_contract_alignment",
    "tests.hedge.optimization.test_memory_optimized_backtesting",
    "tests.hedge.optimization.test_memory_lifecycle",
    "tests.hedge.optimization.test_parallel",
    "tests.hedge.optimization.test_config",
    "tests.hedge.optimization.test_cli_registration",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    rows: list[dict[str, object]] = []

    for module_name in MODULES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            rows.append(
                {
                    "module": module_name,
                    "test": "<import>",
                    "status": "FAIL",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        if suite.countTestCases():
            result = unittest.TestResult()
            suite.run(result)
            problem = {str(test): text for test, text in result.failures + result.errors}
            skipped = {str(test): reason for test, reason in result.skipped}

            def walk(value):
                for item in value:
                    if isinstance(item, unittest.TestSuite):
                        yield from walk(item)
                    else:
                        yield item

            # Reload to enumerate deterministically after suite execution.
            suite2 = unittest.defaultTestLoader.loadTestsFromModule(module)
            for test in walk(suite2):
                name = str(test)
                if name in problem:
                    status, detail = "FAIL", problem[name][-4000:]
                elif name in skipped:
                    status, detail = "SKIP", skipped[name]
                else:
                    status, detail = "PASS", ""
                rows.append(
                    {
                        "module": module_name,
                        "test": name,
                        "status": status,
                        "detail": detail,
                    }
                )

        for name, function in sorted(vars(module).items()):
            if not name.startswith("test_") or not inspect.isfunction(function):
                continue
            if inspect.signature(function).parameters:
                continue
            try:
                function()
                rows.append(
                    {
                        "module": module_name,
                        "test": name,
                        "status": "PASS",
                        "detail": "",
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "module": module_name,
                        "test": name,
                        "status": "FAIL",
                        "detail": "".join(
                            traceback.format_exception(type(exc), exc, exc.__traceback__)
                        )[-4000:],
                    }
                )

    failed = [row for row in rows if row["status"] == "FAIL"]
    passed = [row for row in rows if row["status"] == "PASS"]
    skipped = [row for row in rows if row["status"] == "SKIP"]
    payload = {
        "schema": "freqtrade-hedge-adaptive-cpu-focused-v1",
        "total": len(rows),
        "passed": len(passed),
        "failed": len(failed),
        "skipped": len(skipped),
        "status": "PASS" if not failed else "FAIL",
        "results": rows,
    }
    print(
        f"HEDGE ADAPTIVE CPU FOCUSED: PASS={len(passed)} FAIL={len(failed)} "
        f"SKIP={len(skipped)} TOTAL={len(rows)}"
    )
    if args.output:
        Path(args.output).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
