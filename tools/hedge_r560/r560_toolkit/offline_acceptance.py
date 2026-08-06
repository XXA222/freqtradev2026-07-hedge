from __future__ import annotations
import argparse,json,tempfile,zipfile
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from pathlib import Path
from freqtrade.hedge.r56.admission import AdmissionIntent,IntentAdmissionGate
from freqtrade.hedge.r56.alerts import AlertManager,AlertSeverity
from freqtrade.hedge.r56.allocation import DryRunCapitalAllocator
from freqtrade.hedge.r56.attribution import AttributionInput,PerformanceAttributor
from freqtrade.hedge.r56.breaker import BreakerState,DrawdownCircuitBreaker
from freqtrade.hedge.r56.checkpoint import RuntimeCheckpointManager
from freqtrade.hedge.r56.config import validate_r56_config
from freqtrade.hedge.r56.cooldown import AdaptiveCooldownManager
from freqtrade.hedge.r56.fills import FillOrder,PartialFillEngine
from freqtrade.hedge.r56.ledger import FeeFundingLedger,LedgerEvent,LedgerEventType
from freqtrade.hedge.r56.market import MarketDataHealthGate,MarketHealthInput
from freqtrade.hedge.r56.parameters import ParameterBounds,StrategyParameterRegistry
from freqtrade.hedge.r56.readiness import DryRunReadinessBuilder,ReadinessCheck,SupportBundleBuilder
from freqtrade.hedge.r56.risk import PortfolioRiskMonitor,RiskLeg
from freqtrade.hedge.r56.runtime import DryRunOperationsRuntime,OperationsCycleInput
from freqtrade.hedge.r56.session import RunSession,SessionStatus
from freqtrade.hedge.r56.shadow import ShadowObservation,StrategyShadowComparator
from freqtrade.hedge.r56.slippage import DeterministicSlippageModel
from freqtrade.hedge.r56.state import AtomicRunStateStore
from freqtrade.hedge.r56.universe import PairUniversePolicy
from freqtrade.hedge.r56.warmup import StrategyWarmupGate,WarmupRequirement

def run(project_root:Path)->dict[str,object]:
    t=datetime(2026,8,5,tzinfo=UTC);checks:dict[str,bool]={}
    with tempfile.TemporaryDirectory(prefix='r560-') as td:
        tmp=Path(td)
        s=RunSession.create(account_id='a',symbols=('BTCUSDT',),config={},started_at=t).transition(SessionStatus.RUNNING);s,cid=s.next_cycle(t+timedelta(minutes=1));checks['round_01_session_identity']=cid.endswith('000000000001')
        store=AtomicRunStateStore(tmp/'state.json');store.save(s,{'x':1});checks['round_02_atomic_state']=store.load()[0]==s
        gate=MarketDataHealthGate();checks['round_03_market_health']=gate.evaluate(MarketHealthInput('BTCUSDT',t,t,60,Decimal('100'),Decimal('100'))).ready
        checks['round_04_strategy_warmup']=StrategyWarmupGate(WarmupRequirement(2)).evaluate(base_available=2).ready
        universe=PairUniversePolicy(('BTCUSDT','ETHUSDT'));checks['round_05_pair_universe']=universe.active_symbols(at=t)==('BTCUSDT','ETHUSDT')
        allocation=DryRunCapitalAllocator().allocate(equity=Decimal('1000'),weights={'BTC':Decimal('1')});checks['round_06_capital_allocation']=bool(allocation) and allocation[0].total_notional<=Decimal('500')
        admission=IntentAdmissionGate(min_notional=Decimal('5'),max_notional=Decimal('100'));checks['round_07_intent_admission']=admission.evaluate(AdmissionIntent('i','BTC','LONG',Decimal('1'),Decimal('10')),open_orders=0,new_risk_enabled=True,cooldown_until=None,now=t).approved
        checks['round_08_slippage_model']=DeterministicSlippageModel().quote(side='BUY',reference_price=Decimal('100'),order_notional=Decimal('10'),available_notional=Decimal('100'),volatility=Decimal('1')).execution_price>Decimal('100')
        checks['round_09_partial_fill']=PartialFillEngine().fill(FillOrder('o',Decimal('1'),None,'BUY'),bar_volume=Decimal('10'),market_price=Decimal('100')).filled_quantity>0
        ledger=FeeFundingLedger();ledger.append(LedgerEvent('e',LedgerEventType.REALIZED_PNL,Decimal('1'),'USDT',t));checks['round_10_fee_funding_ledger']=ledger.balance().net==Decimal('1')
        risk=PortfolioRiskMonitor().snapshot(timestamp=t,equity=Decimal('1000'),legs=(RiskLeg('BTC',Decimal('100'),Decimal('100'),Decimal('50')),));checks['round_11_portfolio_risk']=risk.ready
        breaker=DrawdownCircuitBreaker();breaker.evaluate(Decimal('100'));checks['round_12_drawdown_breaker']=breaker.evaluate(Decimal('79')).state is BreakerState.KILLED
        cooldown=AdaptiveCooldownManager();checks['round_13_adaptive_cooldown']=cooldown.record_trade('BTC',pnl=Decimal('-1'),at=t).active
        registry=StrategyParameterRegistry({'risk':ParameterBounds(Decimal('0'),Decimal('1'))});version=registry.create({'risk':Decimal('0.5')},actor='a',at=t);checks['round_14_parameter_registry']=registry.activate(version.version_id)==version
        shadow=StrategyShadowComparator();shadow.record(ShadowObservation('1',Decimal('0'),Decimal('1'),Decimal('0'),Decimal('1'),Decimal('0'),Decimal('1')));checks['round_15_shadow_comparison']=shadow.summary().cycles==1
        manager=RuntimeCheckpointManager(tmp/'cp');cp=manager.create({'x':1},at=t);checks['round_16_checkpoint']=manager.restore(cp.checkpoint_id).payload=={'x':1}
        attr=PerformanceAttributor().calculate(AttributionInput(Decimal('1'),Decimal('1'),Decimal('0'),Decimal('1'),Decimal('0')));checks['round_17_performance_attribution']=attr.net_pnl==Decimal('1')
        alerts=AlertManager(escalate_after=2);alerts.emit(key='x',severity=AlertSeverity.WARNING,message='x',at=t);checks['round_18_alert_management']=alerts.emit(key='x',severity=AlertSeverity.WARNING,message='x',at=t).severity>=AlertSeverity.ERROR
        runtime=DryRunOperationsRuntime(account_id='a',symbols=('BTCUSDT',),config={'r56':{'warmup_candles':1}},state_path=tmp/'runtime.json');snap=runtime.observe(OperationsCycleInput(t,'BTCUSDT',60,Decimal('100'),Decimal('100'),Decimal('1000'),Decimal('1000'),Decimal('10'),Decimal('10'),Decimal('5'),Decimal('0'),Decimal('0'),Decimal('0'),Decimal('0'),Decimal('0'),1,{}));checks['round_19_dashboard_snapshot']=snap.summary()['ready'] is True
        certificate=DryRunReadinessBuilder().build(session_id=runtime.session.session_id,checks=(ReadinessCheck('PASS',True),),at=t);bundle=SupportBundleBuilder().create(destination=tmp/'bundle.zip',certificate=certificate);checks['round_20_readiness_bundle']=certificate.ready and zipfile.is_zipfile(bundle)
        config=json.loads((project_root/'config_examples/config_hedge_r55_dryrun_ui.template.json').read_text(encoding='utf-8'));checks['integration_config']=validate_r56_config(config)==()
    failed=[name for name,value in checks.items() if not value]
    return {'status':'PASS' if not failed else 'FAIL','checks':checks,'passed':sum(checks.values()),'total':len(checks),'failed':failed,'offline_only':True,'exchange_access':'NONE','real_order_requests':0,'mainnet_writes':'LOCKED'}

def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser();parser.add_argument('--project-root',default='.');parser.add_argument('--output');args=parser.parse_args(argv);result=run(Path(args.project_root).resolve());text=json.dumps(result,indent=2,sort_keys=True)
    if args.output:Path(args.output).write_text(text+'\n',encoding='utf-8')
    print(text);return 0 if result['status']=='PASS' else 2
if __name__=='__main__':raise SystemExit(main())
