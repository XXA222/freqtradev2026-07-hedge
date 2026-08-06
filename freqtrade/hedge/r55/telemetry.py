"""Non-authoritative Dry-run dashboard telemetry."""
from __future__ import annotations
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
import json, os
from pathlib import Path
from threading import RLock
from typing import Iterable

@dataclass(frozen=True,slots=True)
class StrategyTelemetry:
    long_score: Decimal=Decimal("0");short_score: Decimal=Decimal("0")
    target_net_quantity: Decimal|None=None;target_net_ratio: Decimal|None=None
    confidence: Decimal=Decimal("1");risk_scale: Decimal=Decimal("1")
    long_exposure_scale: Decimal=Decimal("1");short_exposure_scale: Decimal=Decimal("1")
    allow_new_risk: bool=True;regime: str="UNSPECIFIED";reason: str="";model_version: str="strategy"

@dataclass(frozen=True,slots=True)
class DryRunCycleTelemetry:
    cycle_id:str;account_id:str;symbol:str;timestamp:datetime;mark_price:Decimal
    equity:Decimal;available_balance:Decimal;gross_notional:Decimal;net_quantity:Decimal
    target_net_quantity:Decimal=Decimal("0");net_gap_quantity:Decimal=Decimal("0")
    long_quantity:Decimal=Decimal("0");short_quantity:Decimal=Decimal("0")
    long_target_quantity:Decimal=Decimal("0");short_target_quantity:Decimal=Decimal("0")
    long_average_price:Decimal=Decimal("0");short_average_price:Decimal=Decimal("0")
    unrealized_pnl:Decimal=Decimal("0");realized_pnl:Decimal=Decimal("0")
    funding_pnl:Decimal=Decimal("0");fees:Decimal=Decimal("0")
    ideal_order_count:int=0;submit_order_count:int=0;cancel_order_count:int=0;fill_count:int=0;active_order_count:int=0
    risk_blocked:bool=False;diagnostics:tuple[str,...]=();strategy:StrategyTelemetry=field(default_factory=StrategyTelemetry)
    def __post_init__(self)->None:
        if self.timestamp.tzinfo is None: raise ValueError("telemetry timestamp must be timezone-aware")

class DryRunTelemetryStore:
    def __init__(self,capacity:int=2000)->None:
        if capacity<10 or capacity>100000: raise ValueError("telemetry capacity must be within [10,100000]")
        self.capacity=capacity;self._items:deque[DryRunCycleTelemetry]=deque(maxlen=capacity);self._lock=RLock();self.last_error:str|None=None
    def _record_error(self,exc:Exception)->None:self.last_error=f"{type(exc).__name__}: {exc}"[:512]
    def append(self,item:DryRunCycleTelemetry)->None:
        with self._lock:self._items.append(item)
    def latest(self)->DryRunCycleTelemetry|None:
        with self._lock:return self._items[-1] if self._items else None
    def list(self,limit:int=200)->tuple[DryRunCycleTelemetry,...]:
        with self._lock:return tuple(list(self._items)[-max(1,min(limit,self.capacity)):])
    def clear(self)->None:
        with self._lock:self._items.clear();self.last_error=None

def _encode(value):
    if isinstance(value,Decimal):return str(value)
    if isinstance(value,datetime):return value.astimezone(UTC).isoformat()
    if isinstance(value,tuple):return list(value)
    raise TypeError(type(value).__name__)

def _from(raw:dict)->DryRunCycleTelemetry:
    s=raw.get("strategy",{})
    strategy=StrategyTelemetry(**{k:(Decimal(v) if k in {"long_score","short_score","target_net_quantity","target_net_ratio","confidence","risk_scale","long_exposure_scale","short_exposure_scale"} and v is not None else v) for k,v in s.items()})
    decimal_fields={"mark_price","equity","available_balance","gross_notional","net_quantity","target_net_quantity","net_gap_quantity","long_quantity","short_quantity","long_target_quantity","short_target_quantity","long_average_price","short_average_price","unrealized_pnl","realized_pnl","funding_pnl","fees"}
    defaults={"target_net_quantity":"0","net_gap_quantity":"0","long_target_quantity":"0","short_target_quantity":"0"}
    values={}
    for k,v in raw.items():
        if k=="strategy":continue
        if k=="timestamp":values[k]=datetime.fromisoformat(v)
        elif k in decimal_fields:values[k]=Decimal(str(v))
        elif k=="diagnostics":values[k]=tuple(v)
        else:values[k]=v
    for k,v in defaults.items():values.setdefault(k,Decimal(v))
    values["strategy"]=strategy
    return DryRunCycleTelemetry(**values)

class JsonlDryRunTelemetryStore(DryRunTelemetryStore):
    def __init__(self,path:str|Path,capacity:int=2000,max_bytes:int=16*1024*1024)->None:
        self.path=Path(path);self.max_bytes=max_bytes;super().__init__(capacity);self._load()
    def _load(self)->None:
        if not self.path.exists():return
        try:
            for line in self.path.read_text(encoding="utf-8",errors="replace").splitlines():
                try:super().append(_from(json.loads(line)))
                except Exception:continue
        except OSError as exc:self._record_error(exc)
    def append(self,item:DryRunCycleTelemetry)->None:
        super().append(item)
        try:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self.path.open("a",encoding="utf-8") as f:
                f.write(json.dumps(asdict(item),default=_encode,sort_keys=True,separators=(",",":"))+"\n");f.flush();os.fsync(f.fileno())
            if self.path.stat().st_size>self.max_bytes:self._compact()
        except OSError as exc:self._record_error(exc)
    def _compact(self)->None:
        tmp=self.path.with_suffix(self.path.suffix+".tmp")
        with tmp.open("w",encoding="utf-8") as f:
            for item in self.list(self.capacity):f.write(json.dumps(asdict(item),default=_encode,sort_keys=True,separators=(",",":"))+"\n")
            f.flush();os.fsync(f.fileno())
        tmp.replace(self.path)
    def clear(self)->None:
        super().clear()
        try:
            if self.path.exists():self.path.unlink()
        except OSError as exc:self._record_error(exc)
