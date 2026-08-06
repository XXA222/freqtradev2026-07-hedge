from pathlib import Path
from tools.hedge_r560.r560_toolkit.offline_acceptance import run
from tools.hedge_r560.r560_toolkit.source_audit import run as audit
ROOT=Path(__file__).resolve().parents[3]
def test_offline_acceptance():
    result=run(ROOT);assert result['status']=='PASS' and result['passed']==21
def test_source_audit():
    result=audit(ROOT);assert result['status']=='PASS'
