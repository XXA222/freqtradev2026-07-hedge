"""R5.6 configuration validation with Dry-run-only safety invariants."""
from __future__ import annotations
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_ALLOWED={
    "enabled","state_path","market_max_age_seconds","market_max_gap_candles",
    "mark_index_max_divergence_bps","warmup_candles","informative_warmup",
    "max_gross_ratio","max_margin_ratio","max_net_ratio","drawdown_warning",
    "drawdown_pause","drawdown_kill","drawdown_recovery",
}

def _decimal(raw:object,name:str)->Decimal:
    try:value=Decimal(str(raw))
    except (InvalidOperation,TypeError,ValueError) as exc:raise ValueError(f"{name} must be decimal") from exc
    if not value.is_finite():raise ValueError(f"{name} must be finite")
    return value

def validate_r56_config(config:Mapping[str,Any])->tuple[str,...]:
    errors:list[str]=[];hedge=config.get("hedge",{});hedge=hedge if isinstance(hedge,Mapping) else {};raw=hedge.get("r56",{});raw=raw if isinstance(raw,Mapping) else {}
    if not raw:return ()
    unknown=sorted(set(raw)-_ALLOWED)
    if unknown:errors.append("R56_UNKNOWN_KEYS:"+",".join(unknown))
    enabled=raw.get("enabled",False)
    if not isinstance(enabled,bool):errors.append("R56_ENABLED_MUST_BE_BOOL")
    if enabled:
        if config.get("dry_run") is not True:errors.append("R56_REQUIRES_DRY_RUN")
        if hedge.get("read_only",True) is not True:errors.append("R56_REQUIRES_READ_ONLY")
        if hedge.get("live_trading_enabled",False) is not False:errors.append("R56_FORBIDS_LIVE_TRADING")
    path=str(raw.get("state_path","user_data/hedge/r56/runtime-state.json"))
    if not path.strip():errors.append("R56_STATE_PATH_REQUIRED")
    if ".." in Path(path).parts:errors.append("R56_STATE_PATH_TRAVERSAL")
    for key,minimum in (("market_max_age_seconds",1),("market_max_gap_candles",0),("warmup_candles",1)):
        try:
            value=int(raw.get(key,{"market_max_age_seconds":90,"market_max_gap_candles":2,"warmup_candles":100}[key]))
            if value<minimum:errors.append(f"R56_{key.upper()}_INVALID")
        except Exception:errors.append(f"R56_{key.upper()}_INVALID")
    info=raw.get("informative_warmup",{})
    if not isinstance(info,Mapping):errors.append("R56_INFORMATIVE_WARMUP_INVALID")
    else:
        for timeframe,count in info.items():
            try:
                if not str(timeframe).strip() or int(count)<1:raise ValueError
            except Exception:errors.append(f"R56_INFORMATIVE_WARMUP_INVALID:{timeframe}")
    try:
        gross=_decimal(raw.get("max_gross_ratio","0.80"),"max_gross_ratio");margin=_decimal(raw.get("max_margin_ratio","0.55"),"max_margin_ratio");net=_decimal(raw.get("max_net_ratio","0.50"),"max_net_ratio")
        if any(x<=0 or x>1 for x in (gross,margin,net)):errors.append("R56_RISK_RATIOS_INVALID")
    except ValueError:errors.append("R56_RISK_RATIOS_INVALID")
    try:
        recovery=_decimal(raw.get("drawdown_recovery","0.03"),"drawdown_recovery");warning=_decimal(raw.get("drawdown_warning","0.05"),"drawdown_warning");pause=_decimal(raw.get("drawdown_pause","0.10"),"drawdown_pause");kill=_decimal(raw.get("drawdown_kill","0.20"),"drawdown_kill")
        if not Decimal("0")<=recovery<warning<pause<kill<Decimal("1"):errors.append("R56_DRAWDOWN_THRESHOLDS_INVALID")
    except ValueError:errors.append("R56_DRAWDOWN_THRESHOLDS_INVALID")
    return tuple(dict.fromkeys(errors))
