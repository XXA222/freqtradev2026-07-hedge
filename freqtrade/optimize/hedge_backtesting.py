"""Standard Freqtrade data/strategy adapter for deterministic Hedge backtesting.

The low-level :class:`HedgeBacktesting` facade remains useful for tests and
programmatic event replays.  ``run_freqtrade_hedge_backtest`` adds the missing
production-facing path: normal Freqtrade configuration, downloaded OHLCV,
strategy analysis, futures funding data, the shared Hedge planner/matcher, and a
reproducible JSON result artifact.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import json
from pathlib import Path
from hashlib import sha256
from typing import Any, Iterable, Mapping

from freqtrade.exceptions import OperationalException
from freqtrade.hedge.integration.market_data import exchange_market_rules
from freqtrade.hedge.integration.candle_cursor import bar_fingerprint, missing_candle_count
from freqtrade.hedge.integration.paper_runtime import planner_config_from_mapping
from freqtrade.hedge.integration.signal_provider import signal_from_analyzed_row
from freqtrade.hedge.paper_config import PaperFundingSource, PaperSimulationConfig
from freqtrade.hedge.planning.context import PlannerConfig, StrategyPlanningPort
from freqtrade.hedge.simulation.exchange import (
    BarEvent,
    FundingEvent,
    MarketRules,
    SignalEvent,
    SimulationInputEvent,
    SimulationResult,
)
from freqtrade.hedge.simulation.matcher import MatchConfig
from freqtrade.hedge.simulation.replay import EventReplayEngine


@dataclass(frozen=True, slots=True)
class HedgeBacktestDataset:
    events: tuple[SimulationInputEvent, ...]
    pair: str
    timeframe: str
    start: datetime
    end: datetime
    bar_count: int
    signal_count: int
    funding_count: int
    missing_candle_count: int = 0
    data_fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class HedgeBacktestRun:
    result: SimulationResult
    dataset: HedgeBacktestDataset
    export_path: Path
    strategy: str
    market_rule_source: str
    market_rule_version: str
    artifact_sha256: str = ""
    result_fingerprint: str = ""


def _aware(value: object, *, field: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise OperationalException(f"{field} must contain datetime values")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise OperationalException(f"{field} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OperationalException(f"{field} must be a valid decimal") from exc
    if not result.is_finite():
        raise OperationalException(f"{field} must be finite")
    return result


def _strict_dates(frame: Any, *, field: str = "date") -> tuple[datetime, ...]:
    if field not in getattr(frame, "columns", ()):
        raise OperationalException(f"Hedge backtest dataframe is missing {field!r}")
    values = tuple(_aware(value, field=field) for value in frame[field].tolist())
    if not values:
        raise OperationalException("Hedge backtest has no analyzed candles")
    if any(right <= left for left, right in zip(values, values[1:], strict=False)):
        raise OperationalException(
            "Hedge backtest candles must be strictly chronological and unique"
        )
    return values


def events_from_analyzed_dataframe(
    *,
    pair: str,
    timeframe: str,
    frame: Any,
    funding_frame: Any | None = None,
    strategy_version: object = None,
    require_funding_data: bool = False,
    max_missing_candles: int = 0,
) -> HedgeBacktestDataset:
    """Build the shared event stream from one analyzed Freqtrade dataframe.

    A signal and bar share the candle close timestamp.  The engine first matches
    orders accepted on earlier bars and only then creates orders from the current
    signal, so the current OHLC path can never fill an order derived from its own
    close or indicators.
    """

    if frame is None or getattr(frame, "empty", True):
        raise OperationalException("Hedge backtest analyzed dataframe is empty")
    required = {"date", "open", "high", "low", "close"}
    columns = set(getattr(frame, "columns", ()))
    missing = sorted(required - columns)
    if missing:
        raise OperationalException(
            "Hedge backtest analyzed dataframe is missing: " + ", ".join(missing)
        )
    signal_columns = {
        "hedge_long_score",
        "hedge_short_score",
        "hedge_target_net",
        "enter_long",
        "enter_short",
    }
    if not signal_columns.intersection(columns):
        raise OperationalException(
            "Hedge backtest strategy produced no hedge_* or enter_long/enter_short columns"
        )
    dates = _strict_dates(frame)
    missing_slots = sum(
        missing_candle_count(left, right, timeframe)
        for left, right in zip(dates, dates[1:], strict=False)
    )
    if missing_slots > max_missing_candles:
        raise OperationalException(
            f"Hedge backtest has {missing_slots} missing candle slots; "
            f"limit={max_missing_candles}"
        )

    events: list[SimulationInputEvent] = []
    bars: list[BarEvent] = []
    for _, row in frame.iterrows():
        signal = signal_from_analyzed_row(
            pair=pair,
            timeframe=timeframe,
            row=row,
            columns=columns,
            feature_timestamp=_aware(row.get("date"), field="date"),
            strategy_version=strategy_version,
        )
        candle = signal.candle
        if candle is None:  # guarded by the required columns above
            raise OperationalException("Hedge backtest could not build an OHLCV candle")
        events.append(
            SignalEvent(
                timestamp=signal.candle_close_time,
                symbol=pair,
                long_signal=signal.long_score,
                short_signal=signal.short_score,
                target_net=signal.target_net,
                model_version=signal.model_version,
                reason=signal.strategy_reason or signal.reason,
                target_net_ratio=signal.target_net_ratio,
                confidence=signal.confidence,
                risk_scale=signal.risk_scale,
                long_exposure_scale=signal.long_exposure_scale,
                short_exposure_scale=signal.short_exposure_scale,
                allow_new_risk=signal.allow_new_risk,
                regime=signal.regime,
            )
        )
        bar = candle.to_bar_event()
        bars.append(bar)
        events.append(bar)

    start = bars[0].timestamp
    end = bars[-1].timestamp
    funding_count = 0
    funding_missing = funding_frame is None or getattr(funding_frame, "empty", True)
    if require_funding_data and funding_missing:
        raise OperationalException(
            "Hedge futures backtest requires downloaded funding/mark data; "
            "run download-data with the futures configuration first"
        )
    if not funding_missing:
        funding_columns = set(getattr(funding_frame, "columns", ()))
        required_funding = {"date", "open_fund", "open_mark"}
        if not required_funding.issubset(funding_columns):
            raise OperationalException(
                "Futures funding data must contain date/open_fund/open_mark"
            )
        funding_dates = _strict_dates(funding_frame)
        for (_, row), timestamp in zip(
            funding_frame.iterrows(), funding_dates, strict=True
        ):
            if timestamp < start or timestamp > end:
                continue
            rate = _decimal(row.get("open_fund"), field="funding.open_fund")
            mark = _decimal(row.get("open_mark"), field="funding.open_mark")
            if mark <= 0:
                raise OperationalException("funding.open_mark must be positive")
            events.append(
                FundingEvent(
                    timestamp=timestamp,
                    symbol=pair,
                    rate=rate,
                    mark_price=mark,
                )
            )
            funding_count += 1

    fingerprint_payload = "\n".join(bar_fingerprint(item) for item in bars).encode("utf-8")
    data_fingerprint = sha256(fingerprint_payload).hexdigest()
    return HedgeBacktestDataset(
        events=tuple(events),
        pair=pair,
        timeframe=timeframe,
        start=start,
        end=end,
        bar_count=len(bars),
        signal_count=len(bars),
        funding_count=funding_count,
        missing_candle_count=missing_slots,
        data_fingerprint=data_fingerprint,
    )


class HedgeBacktesting:
    """Freqtrade-facing adapter over the shared deterministic event engine."""

    def __init__(
        self,
        *,
        initial_balance: Decimal,
        planner_config: PlannerConfig | None = None,
        leverage: Decimal = Decimal("3"),
        fee_rate: Decimal = Decimal("0.0004"),
        long_signal: Decimal = Decimal("1"),
        short_signal: Decimal = Decimal("1"),
        target_net_quantity: Decimal | None = None,
        market_rules: MarketRules | None = None,
        planner: StrategyPlanningPort | None = None,
        match_config: MatchConfig | None = None,
    ) -> None:
        self.engine = EventReplayEngine(
            initial_balance=initial_balance,
            planner_config=planner_config,
            leverage=leverage,
            fee_rate=fee_rate,
            long_signal=long_signal,
            short_signal=short_signal,
            target_net_quantity=target_net_quantity,
            market_rules=market_rules,
            planner=planner,
            match_config=match_config,
        )

    def run(self, events: Iterable[SimulationInputEvent]) -> SimulationResult:
        return self.engine.replay(events)


def _paper_match_config(config: PaperSimulationConfig) -> MatchConfig:
    return MatchConfig(
        maker_fee_rate=config.maker_fee_rate,
        taker_fee_rate=config.taker_fee_rate,
        volume_participation=config.volume_participation,
        market_slippage_bps=config.market_slippage_bps,
        price_tick=config.tick_size,
        qty_step=config.qty_step,
        min_fill_qty=config.min_qty,
        min_fill_notional=config.min_notional,
        max_entry_layers_per_bar=config.max_entry_layers_per_bar,
        max_reduce_layers_per_bar=config.max_reduce_layers_per_bar,
        max_fill_ratio_per_order=config.max_fill_ratio_per_order,
        max_fills_per_bar=config.max_fills_per_bar,
    )


def _json_value(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _default_export_path(config: Mapping[str, Any]) -> Path:
    user_data = Path(str(config.get("user_data_dir", "user_data")))
    directory = user_data / "backtest_results"
    stamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    return directory / f"hedge-backtest-{stamp}.json"


def _write_result(
    *,
    path: Path,
    result: SimulationResult,
    dataset: HedgeBacktestDataset,
    strategy: str,
    market_rule_source: str,
    market_rule_version: str,
    export_events: bool,
) -> tuple[str, str]:
    deterministic_payload: dict[str, object] = {
        "schema_version": "hedge-backtest-result-v3",
        "execution_timing": "NEXT_BAR_NO_LOOKAHEAD",
        "pair": dataset.pair,
        "timeframe": dataset.timeframe,
        "start": dataset.start,
        "end": dataset.end,
        "strategy": strategy,
        "market_rule_source": market_rule_source,
        "market_rule_version": market_rule_version,
        "bar_count": dataset.bar_count,
        "signal_count": dataset.signal_count,
        "funding_count": dataset.funding_count,
        "missing_candle_count": dataset.missing_candle_count,
        "data_fingerprint": dataset.data_fingerprint,
        "report": result.report,
        "snapshots": result.snapshots,
    }
    if export_events:
        deterministic_payload["events"] = result.events
    canonical = json.dumps(
        _json_value(deterministic_payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    result_fingerprint = sha256(canonical).hexdigest()
    payload = {
        **deterministic_payload,
        "created_at": datetime.now(UTC),
        "result_fingerprint": result_fingerprint,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(_json_value(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp.replace(path)
    digest = sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar_temp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    sidecar_temp.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    sidecar_temp.replace(sidecar)
    return digest, result_fingerprint


def run_freqtrade_hedge_backtest(
    config: dict[str, Any],
    *,
    export_path: Path | None = None,
    export_events: bool = False,
) -> HedgeBacktestRun:
    """Run a single-pair Hedge backtest through Freqtrade's normal data stack."""

    from freqtrade.data.converter import trim_dataframe
    from freqtrade.hedge.config import validate_hedge_config
    from freqtrade.optimize.backtesting import Backtesting

    hedge_runtime = validate_hedge_config(config)
    paper = hedge_runtime.paper
    if not hedge_runtime.enabled or paper is None:
        raise OperationalException("hedge-backtesting requires Hedge mode and hedge.paper")
    if hedge_runtime.operation_mode not in {"paper", "shadow"}:
        raise OperationalException(
            "hedge-backtesting requires hedge.operation_mode=paper or shadow"
        )

    backend = Backtesting(config)
    with backend.progress or nullcontext():
        data, timerange = backend.load_bt_data()
        if len(backend.strategylist) != 1:
            raise OperationalException(
                "hedge-backtesting currently supports exactly one strategy per run"
            )
        strategy = backend.strategylist[0]
        backend._set_strategy(strategy)
        pair = hedge_runtime.managed_pair
        if pair is None or pair not in data:
            raise OperationalException(
                f"managed_pair {pair!r} is not available in the loaded backtest data"
            )
        analyzed = strategy.advise_all_indicators(data)
        frame = strategy.ft_advise_signals(analyzed[pair], {"pair": pair})
        frame = trim_dataframe(
            frame,
            timerange,
            startup_candles=backend.required_startup,
        ).copy()

    if len(frame) < 2:
        raise OperationalException(
            "hedge-backtesting requires at least two analyzed candles for next-bar execution"
        )
    version_attr = getattr(strategy, "version", None)
    strategy_version = version_attr() if callable(version_attr) else version_attr
    dataset = events_from_analyzed_dataframe(
        pair=pair,
        timeframe=backend.timeframe,
        frame=frame,
        funding_frame=backend.futures_data.get(pair),
        strategy_version=strategy_version,
        require_funding_data=paper.funding_source is PaperFundingSource.EXCHANGE,
        max_missing_candles=paper.max_missing_candles,
    )
    raw_hedge = config.get("hedge", {})
    hedge_mapping = raw_hedge if isinstance(raw_hedge, Mapping) else {}
    planner_raw = hedge_mapping.get("planner", {})
    planner_mapping = planner_raw if isinstance(planner_raw, Mapping) else {}
    paper_raw = hedge_mapping.get("paper", {})
    paper_mapping = paper_raw if isinstance(paper_raw, Mapping) else {}
    rule_snapshot = exchange_market_rules(
        exchange=backend.exchange,
        pair=pair,
        fallback=paper_mapping,
    )
    market_rules = MarketRules(
        tick_size=rule_snapshot.tick_size,
        qty_step=rule_snapshot.qty_step,
        min_qty=rule_snapshot.min_qty,
        min_notional=rule_snapshot.min_notional,
    )
    match_config = _paper_match_config(paper)
    match_config = MatchConfig(
        **{
            **asdict(match_config),
            "maker_fee_rate": rule_snapshot.maker_fee_rate,
            "taker_fee_rate": rule_snapshot.taker_fee_rate,
            "price_tick": rule_snapshot.tick_size,
            "qty_step": rule_snapshot.qty_step,
            "min_fill_qty": rule_snapshot.min_qty,
            "min_fill_notional": rule_snapshot.min_notional,
        }
    )
    runner = HedgeBacktesting(
        initial_balance=paper.initial_balance,
        planner_config=planner_config_from_mapping(planner_mapping),
        leverage=paper.leverage,
        fee_rate=rule_snapshot.taker_fee_rate,
        long_signal=paper.default_long_signal,
        short_signal=paper.default_short_signal,
        market_rules=market_rules,
        match_config=match_config,
    )
    result = runner.run(dataset.events)
    output = (export_path or _default_export_path(config)).expanduser().resolve()
    strategy_name = strategy.get_strategy_name()
    artifact_sha256, result_fingerprint = _write_result(
        path=output,
        result=result,
        dataset=dataset,
        strategy=strategy_name,
        market_rule_source=rule_snapshot.source,
        market_rule_version=rule_snapshot.version,
        export_events=export_events,
    )
    return HedgeBacktestRun(
        result=result,
        dataset=dataset,
        export_path=output,
        strategy=strategy_name,
        market_rule_source=rule_snapshot.source,
        market_rule_version=rule_snapshot.version,
        artifact_sha256=artifact_sha256,
        result_fingerprint=result_fingerprint,
    )
