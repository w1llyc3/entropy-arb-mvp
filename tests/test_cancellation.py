import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import sqlite3

import pytest

from entropy_arb.execution import OfflineExecutor
from entropy_arb.execution_contract import OrderReport, OrderState
from entropy_arb.storage import Ledger, IdempotencyConflict, RecoveryRequired
from fakes.venues import FakeVenue
from test_storage import legs


def open_order(ledger, venues):
    from dataclasses import replace
    specs=[replace(leg,account=venues[leg.venue].account) for leg in legs()]
    op=ledger.create_intent('open','proposal','open',specs)
    ledger.claim(op)
    records=ledger.operation(op)['legs']
    for leg in records:
        v=venues[leg['venue']]
        v.state.intents[leg['client_id']]=OfflineExecutor.intent(leg)
        report=OrderReport(leg['client_id'],OrderState.OPEN,authoritative=True)
        v.state.reports[leg['client_id']]=report
        ledger.save_report(leg['id'],report)
    return op,records[0]


@pytest.fixture
def context(tmp_path):
    ledger=Ledger(tmp_path/'ledger.sqlite')
    venues={n:FakeVenue(venue=n,account='fake-account-'+n[0]) for n in ('entropy','lighter')}
    executor=OfflineExecutor(ledger,venues,allow_submit=True)
    op,leg=open_order(ledger,venues)
    yield ledger,venues,executor,op,leg
    ledger.close()


def test_cancel_ack_requires_query_and_preserves_reservation(context):
    ledger,venues,executor,op,leg=context
    result=asyncio.run(executor.cancel('cancel',leg['id']))
    assert result['state']=='VERIFIED'
    stored=ledger.operation(op)['legs'][0]
    assert stored['outcome']=='canceled' and D(stored['reservation'])>0
    assert ledger.operation(op)['state']!='SETTLED'
    assert venues[leg['venue']].cancel_calls==1
    assert sum(v.submit_calls for v in venues.values())==0
    assert asyncio.run(executor.cancel('cancel',leg['id']))==result
    assert venues[leg['venue']].cancel_calls==1


def test_lost_ack_and_fill_racing_cancel_never_invents_zero(context):
    ledger,venues,executor,op,leg=context
    venue=venues[leg['venue']]
    async def race(client_id):
        venue.cancel_calls+=1
        report=OrderReport(client_id,OrderState.CANCELED,D('.004'),True)
        venue.state.reports[client_id]=report
        venue.state.events.append(('late-fill',report))
        raise TimeoutError('sensitive transport payload')
    venue.cancel=race
    assert asyncio.run(executor.cancel('cancel',leg['id']))['state']=='VERIFIED'
    assert D(ledger.operation(op)['legs'][0]['filled'])==D('.004')
    assert ledger.operation(op)['state']!='SETTLED'


@pytest.mark.parametrize('fault',['ack_only','lost_ack','query_error'])
def test_inconclusive_cancel_never_releases_or_resends(context,fault):
    ledger,venues,executor,op,leg=context
    venue=venues[leg['venue']]
    async def cancel(client_id):
        venue.cancel_calls+=1
        if fault=='lost_ack': raise TimeoutError()
    venue.cancel=cancel
    if fault=='query_error':
        async def query(client_id): raise ConnectionError()
        venue.query=query
    result=asyncio.run(executor.cancel('cancel',leg['id']))
    assert result['state']==('UNKNOWN' if fault=='lost_ack' else 'ACKNOWLEDGED')
    assert ledger.operation(op)['state']!='SETTLED'
    assert all(D(x['reservation'])>0 for x in ledger.operation(op)['legs'])
    asyncio.run(executor.cancel('cancel',leg['id']))
    assert venue.cancel_calls==1
    with pytest.raises(RecoveryRequired):
        asyncio.run(executor.cancel('different-key',leg['id']))


def test_cancel_key_cannot_change_order(context):
    ledger,_,executor,op,leg=context
    asyncio.run(executor.cancel('cancel',leg['id']))
    with pytest.raises(IdempotencyConflict):
        asyncio.run(executor.cancel('cancel',ledger.operation(op)['legs'][1]['id']))


def test_cancel_unknown_recovers_after_delayed_order_index_without_retry(context):
    ledger,venues,executor,op,leg=context
    venue=venues[leg['venue']]
    venue.fault='lose_cancel_ack'; venue.query_lag=1
    assert asyncio.run(executor.cancel('cancel',leg['id']))['state']=='UNKNOWN'
    assert ledger.metrics()['pending_cancellations']==1
    asyncio.run(executor.recover())
    assert ledger.cancellation('cancel')['state']=='VERIFIED'
    assert ledger.metrics()['pending_cancellations']==0
    assert venue.cancel_calls==1


def test_ack_record_commit_failure_preserves_query_only_recovery(context,monkeypatch):
    ledger,venues,executor,op,leg=context
    def disk_full(*a,**kw): raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(ledger,'cancel_ack',disk_full)
    with pytest.raises(sqlite3.OperationalError): asyncio.run(executor.cancel('cancel',leg['id']))
    assert ledger.cancellation('cancel')['state']=='SUBMITTING'
    asyncio.run(executor.recover())
    assert ledger.cancellation('cancel')['state']=='VERIFIED'
    assert venues[leg['venue']].cancel_calls==1


def test_cancel_send_requires_intent_commit(context,monkeypatch):
    ledger,venues,executor,_,leg=context
    def disk_full(*a,**kw): raise sqlite3.OperationalError('disk full')
    monkeypatch.setattr(ledger,'prepare_cancel',disk_full)
    with pytest.raises(sqlite3.OperationalError): asyncio.run(executor.cancel('cancel',leg['id']))
    assert sum(v.cancel_calls for v in venues.values())==0


def test_service_cancel_two_open_legs_reconciles_before_settlement(tmp_path):
    from test_service import setup
    async def run():
        svc,venues,_=setup(tmp_path)
        await svc.start()
        try:
            op,leg=open_order(svc.ledger,venues)
            # Same verified account baselines as a service-created intent.
            with svc.ledger.transaction() as db:
                import json
                for name,snapshot in svc.recovery.snapshots.items():
                    db.execute('INSERT INTO baselines VALUES(?,?,?)',(op,name,json.dumps(snapshot.payload())))
            for i,record in enumerate(svc.ledger.operation(op)['legs']):
                payload=dict(confirm=True,leg_id=record['id'],idempotency_key='cancel-'+str(i))
                result=await svc.call('cancel_order',payload)
                assert result['cancellation']['state']=='VERIFIED'
                assert result['reconciled'] is (i==1)
                await svc.call('cancel_order',payload)
            assert svc.ledger.operation(op)['state']=='SETTLED'
            assert all(D(r['reservation'])==0 for r in svc.ledger.operation(op)['legs'])
            assert all(v.cancel_calls==1 and v.submit_calls==0 for v in venues.values())
        finally: await svc.close()
    asyncio.run(run())


def test_crash_after_cancel_boundary_recovers_by_query_only(context):
    ledger,venues,_,op,leg=context
    def crash(stage,value):
        if stage=='before_cancel': raise RuntimeError('process stopped')
    executor=OfflineExecutor(ledger,venues,allow_submit=True,hook=crash)
    with pytest.raises(RuntimeError): asyncio.run(executor.cancel('cancel',leg['id']))
    assert ledger.cancellation('cancel')['state']=='SUBMITTING'
    # Reopen storage as on restart. No cancellation transport has run yet.
    path=ledger.db.execute('PRAGMA database_list').fetchone()[2]
    restarted=Ledger(path)
    try:
        replacement=OfflineExecutor(restarted,venues,allow_submit=True)
        asyncio.run(replacement.recover())
        asyncio.run(replacement.cancel('cancel',leg['id']))
        assert venues[leg['venue']].cancel_calls==0
        assert restarted.cancellation('cancel')['state']=='SUBMITTING'
    finally: restarted.close()


def test_cancel_claim_is_atomic_across_clients(context):
    ledger,_,_,_,leg=context
    ledger.prepare_cancel('cancel',leg['id'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(lambda _:ledger.claim_cancel('cancel'),range(30)))==1


def test_cancel_default_transport_disabled(context):
    ledger,venues,_,_,leg=context
    executor=OfflineExecutor(ledger,venues)
    with pytest.raises(ValueError): asyncio.run(executor.cancel('cancel',leg['id']))
    assert all(v.cancel_calls==0 for v in venues.values())


def test_v2_migration_backs_up_original_and_keeps_order_ids(tmp_path):
    path=tmp_path/'ledger.sqlite'
    ledger=Ledger(path)
    op=ledger.create_intent('key','proposal','open',legs())
    before=ledger.operation(op)
    ledger.db.execute('DROP TABLE cancellations')
    ledger.db.execute('PRAGMA user_version=2')
    ledger.close()
    upgraded=Ledger(path)
    try:
        assert upgraded.operation(op)==before
        assert upgraded.db.execute('PRAGMA user_version').fetchone()[0]==3
        backups=list(tmp_path.glob('ledger.sqlite.pre-v3-*.sqlite'))
        assert len(backups)==1
        with sqlite3.connect(backups[0]) as backup:
            assert backup.execute('PRAGMA user_version').fetchone()[0]==2
            assert backup.execute('SELECT id FROM operations').fetchone()[0]==op
    finally: upgraded.close()


def test_actual_api_keeps_proposal_cancel_separate_and_real_cancel_disabled(tmp_path):
    from fastapi.testclient import TestClient
    from entropy_arb.service import TradingService
    from web.panel import create_app
    svc=TradingService(tmp_path,lock_dir=tmp_path/'locks')
    with TestClient(create_app(tmp_path,service=svc)) as client:
        assert client.post('/api/confirm/cancel').status_code==200
        response=client.post('/api/orders/cancel',json=dict(confirm=True,idempotency_key='cancel',leg_id='x'))
        assert response.status_code==409
        assert 'RECORD_ONLY' in response.text


@pytest.mark.parametrize('phase',['before_cancel','exchange_accepted','after_cancel_ack'])
def test_cancel_process_exit_reopens_with_same_identity_and_never_retries(tmp_path,phase):
    import json
    import subprocess
    import sys
    from pathlib import Path
    path=tmp_path/'ledger.sqlite'; evidence=tmp_path/'fake-exchange.json'
    store=Ledger(path)
    venues={n:FakeVenue(venue=n) for n in ('entropy','lighter')}
    op,leg=open_order(store,venues)
    store.close()
    code=r'''
import asyncio,json,os,sys
from entropy_arb.execution import OfflineExecutor
from entropy_arb.storage import Ledger
path,leg_id,evidence,phase=sys.argv[1:]
store=Ledger(path)
class Venue:
    is_simulation=True
    async def cancel(self,client_id):
        with open(evidence,'w') as f:
            json.dump({'client_id':client_id},f); f.flush(); os.fsync(f.fileno())
        if phase=='exchange_accepted': os._exit(73)
def hook(stage,value):
    if stage==phase: os._exit(73)
asyncio.run(OfflineExecutor(store,{'entropy':Venue(),'lighter':Venue()},allow_submit=True,hook=hook).cancel('cancel',leg_id))
'''
    result=subprocess.run([sys.executable,'-c',code,str(path),leg['id'],str(evidence),phase],
                          cwd=Path(__file__).parents[1],capture_output=True,timeout=15)
    assert result.returncode==73,result.stderr
    reopened=Ledger(path)
    accepted=json.loads(evidence.read_text()) if evidence.exists() else None
    class QueryOnly:
        is_simulation=True
        async def cancel(self,client_id): pytest.fail('restart must not retry cancellation')
        async def submit(self,intent): pytest.fail('restart must not replace an order')
        async def query(self,client_id):
            state=OrderState.CANCELED if accepted and accepted['client_id']==client_id else OrderState.OPEN
            return OrderReport(client_id,state,authoritative=True)
    try:
        executor=OfflineExecutor(reopened,{n:QueryOnly() for n in venues},allow_submit=True)
        asyncio.run(executor.recover())
        assert reopened.operation(op)['legs'][0]['client_id']==leg['client_id']
        expected='SUBMITTING' if phase=='before_cancel' else 'VERIFIED'
        assert asyncio.run(executor.cancel('cancel',leg['id']))['state']==expected
        assert all(D(x['reservation'])>0 for x in reopened.operation(op)['legs'])
    finally: reopened.close()
