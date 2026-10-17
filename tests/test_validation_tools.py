import json
from pathlib import Path
import subprocess
import sys

import pytest

from entropy_arb.live_transport import read_operator_env,verify_sdk_pins,inspect_operator_env


def test_read_auth_loads_only_lighter_not_hl_secret(tmp_path):
    env=tmp_path/'synthetic.env'
    env.write_text('ENTROPY_HL_PRIVATE_KEY=hl-sentinel\nLIGHTER_API_PRIVATE_KEY=lighter-sentinel\nLIGHTER_API_KEY_INDEX=2\n')
    selected=read_operator_env(env,lighter_auth=True)
    assert 'ENTROPY_HL_PRIVATE_KEY' not in selected
    assert selected['LIGHTER_API_PRIVATE_KEY']=='lighter-sentinel'


def test_unpinned_signing_installation_cannot_pass_preflight(monkeypatch):
    class Installed:
        def read_text(self,name): return '{}'
    monkeypatch.setattr('entropy_arb.live_transport.metadata.distribution',lambda name:Installed())
    with pytest.raises(RuntimeError): verify_sdk_pins()


def test_legacy_env_aliases_keep_secrets_out_of_read_only_values(tmp_path):
    env=tmp_path/'synthetic.env'
    env.write_text('HL_ACCOUNT_ADDRESS=public-address\nHL_PRIVATE_KEY=secret-sentinel\n')
    selected=read_operator_env(env)
    assert selected=={'ENTROPY_HL_ACCOUNT':'public-address'}
    report=inspect_operator_env(env)
    assert report['required_present']['ENTROPY_HL_PRIVATE_KEY']
    assert 'secret-sentinel' not in json.dumps(report)


def test_conflicting_legacy_and_new_name_does_not_silently_choose_key(tmp_path):
    env=tmp_path/'synthetic.env'
    env.write_text('HL_ACCOUNT_ADDRESS=first\nENTROPY_HL_ACCOUNT=second\n')
    with pytest.raises(ValueError,match='duplicate'): read_operator_env(env)
    assert inspect_operator_env(env)['duplicate_fields']==['ENTROPY_HL_ACCOUNT']


@pytest.mark.parametrize('value',['','"" # comment',"''",'# nothing'])
def test_empty_env_values_do_not_count_as_configured(tmp_path,value):
    env=tmp_path/'synthetic.env'
    env.write_text('HL_PRIVATE_KEY='+value+'\n')
    assert not inspect_operator_env(env)['required_present']['ENTROPY_HL_PRIVATE_KEY']


def test_no_real_sleep_is_inconclusive_not_success(tmp_path):
    out=tmp_path/'probe.json'
    result=subprocess.run([sys.executable,'tools/windows_resume_probe.py','--seconds','1','--output',str(out)],
        cwd=Path(__file__).parents[1],capture_output=True,timeout=15)
    assert result.returncode==2,result.stderr
    report=json.loads(out.read_text())
    assert report['submits']==0 and not report['gap_observed'] and not report['checks_passed']


def test_soak_preserves_database_evidence_and_never_calls_short_run_24h(tmp_path):
    import hashlib
    out=tmp_path/'soak.json'
    result=subprocess.run([sys.executable,'tools/soak_execution.py','--seconds','1','--cycle-sec','5','--output',str(out)],
        cwd=Path(__file__).parents[1],capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    report=json.loads(out.read_text())
    assert report['passed'] and not report['running'] and not report['qualifies_as_24h_soak']
    ledger=Path(report['state_dir'])/'.web/ledger.sqlite'
    assert hashlib.sha256(ledger.read_bytes()).hexdigest()==report['ledger_sha256']


def test_rest_query_bool_is_wire_string_and_auth_is_not_in_error():
    import asyncio
    from entropy_arb.live_transport import RestTransport,ExchangeReadError
    calls=[]
    class Response:
        status=403
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
    class Session:
        def request(self,method,url,**kw):
            calls.append(kw)
            return Response()
    async def run():
        with pytest.raises(ExchangeReadError) as caught:
            await RestTransport(Session(),'secret-token').lighter('trades',dict(aggregate=False,limit=1))
        assert caught.value.status==403 and caught.value.endpoint=='/api/v1/trades'
        assert 'secret-token' not in str(caught.value)
    asyncio.run(run())
    assert calls[0]['params']['aggregate']=='false'


def test_empty_history_response_is_not_completeness_proof():
    import asyncio
    from entropy_arb.live_check import check_read_contracts
    from types import SimpleNamespace
    class Rest:
        async def lighter(self,path,params): return dict(code=200,orders=[],trades=[])
        async def info(self,payload): return []
    report=asyncio.run(check_read_contracts(Rest(),dict(entropy='0x'+'1'*40,lighter='7'),
        dict(lighter=SimpleNamespace(market_id=139))))
    assert not report['history_completeness_verified'] and not report['settlement_contract_verified']
    assert not report['lighter_trade_sample_present']
