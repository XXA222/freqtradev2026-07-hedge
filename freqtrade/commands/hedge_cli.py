"""Hedge CLI registration isolated from Freqtrade's upstream parser layout."""

from __future__ import annotations

from argparse import ArgumentParser, _SubParsersAction
from typing import Any


def _start_hedge_db(args: dict[str, Any]) -> Any:
    """Load the Hedge database command only when it is executed."""
    from freqtrade.commands.hedge_db_commands import start_hedge_db

    return start_hedge_db(args)


def _start_hedge_backtesting(args: dict[str, Any]) -> Any:
    """Load the Hedge backtesting runtime only when it is executed."""
    from freqtrade.commands.hedge_runtime_commands import start_hedge_backtesting

    return start_hedge_backtesting(args)


def _start_hedge_paper(args: dict[str, Any]) -> Any:
    """Load the durable Hedge Paper runtime only when it is executed."""
    from freqtrade.commands.hedge_runtime_commands import start_hedge_paper

    return start_hedge_paper(args)


def _start_hedge_readonly_check(args: dict[str, Any]) -> Any:
    """Load the Binance readonly preflight only when it is executed."""
    from freqtrade.commands.hedge_readonly_commands import start_hedge_readonly_check

    return start_hedge_readonly_check(args)


def register_hedge_subcommands(
    manager: Any,
    subparsers: _SubParsersAction,
    common_parser: ArgumentParser,
    strategy_parser: ArgumentParser,
    *,
    trade_options: list[str],
    backtest_options: list[str],
) -> None:
    """Idempotently register Hedge-only commands without importing runtimes."""

    choices = getattr(subparsers, "choices", {})
    if "hedge-paper" not in choices:
        hedge_paper_cmd = subparsers.add_parser(
            "hedge-paper",
            help="Run SQL-durable Hedge Paper with real DataProvider OHLCV.",
            parents=[common_parser, strategy_parser],
        )
        hedge_paper_cmd.set_defaults(func=_start_hedge_paper)
        manager._build_args(optionlist=trade_options, parser=hedge_paper_cmd)

    if "hedge-backtesting" not in choices:
        hedge_backtesting_cmd = subparsers.add_parser(
            "hedge-backtesting",
            help="Backtest dual-leg Hedge planning with next-bar execution.",
            parents=[common_parser, strategy_parser],
        )
        hedge_backtesting_cmd.set_defaults(func=_start_hedge_backtesting)
        manager._build_args(optionlist=backtest_options, parser=hedge_backtesting_cmd)
        hedge_backtesting_cmd.add_argument(
            "--hedge-export-filename",
            dest="hedge_export_filename",
            help="Write the Hedge result JSON to this path.",
        )
        hedge_backtesting_cmd.add_argument(
            "--hedge-export-events",
            dest="hedge_export_events",
            action="store_true",
            default=False,
            help="Include the full event ledger in the result JSON.",
        )

    if "hedge-db" not in choices:
        hedge_db = subparsers.add_parser(
            "hedge-db",
            help="Plan, migrate, or verify the explicitly gated Hedge schema.",
            parents=[common_parser],
        )
        hedge_db.set_defaults(func=_start_hedge_db)
        hedge_db.add_argument(
            "--action",
            dest="hedge_db_action",
            choices=("status", "plan", "migrate", "verify"),
            default="status",
        )
        hedge_db.add_argument("--db-url", dest="db_url")
        hedge_db.add_argument(
            "--backup-directory",
            dest="hedge_backup_directory",
        )

    if "hedge-readonly-check" not in choices:
        readonly_check = subparsers.add_parser(
            "hedge-readonly-check",
            help="Run a fail-closed Binance REST-only account preflight.",
            parents=[common_parser],
        )
        readonly_check.set_defaults(func=_start_hedge_readonly_check)
        readonly_check.add_argument(
            "--output",
            dest="hedge_readonly_output",
            help="Write the sanitized preflight JSON report to this path.",
        )
        readonly_check.add_argument(
            "--include-history",
            dest="hedge_readonly_include_history",
            action="store_true",
            default=False,
            help="Also collect order, fill and income history during preflight.",
        )
