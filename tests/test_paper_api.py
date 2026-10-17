import time
from fastapi.testclient import TestClient

from entropy_arb.workbench import SessionStore
from web.workbench import create_app
from test_paper_session import books


class PublicFeedFixture:
    def __init__(self):
        self.market=books()
        self.books={}
        self.symbol=None
    async def select(self,symbol,http):
        self.symbol=symbol
        self.books=self.market if symbol else {}
    async def close(self):
        self.books={}


def wait(client,predicate):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        status=client.get('/api/status').json()
        if predicate(status):return status
        time.sleep(.02)
    raise AssertionError('paper background condition not reached')


def test_background_simulation_survives_http_polling_and_drains_on_end(tmp_path,monkeypatch):
    import entropy_arb.live_transport as transport
    from web.recorder_ctl import RecorderControl
    def forbidden(*a,**kw):raise AssertionError('real transport/collector touched')
    monkeypatch.setattr(transport,'read_operator_env',forbidden)
    monkeypatch.setattr(RecorderControl,'stop',forbidden)
    feed=PublicFeedFixture()
    app=create_app(state_dir=tmp_path/'state',data_root=tmp_path/'collector',paper_feed=feed)
    with TestClient(app) as client:
        s=client.post('/api/sessions',json=dict(key='create-api-paper',rules=dict(run_mode='PAPER'))).json()
        sid=s['id']
        assert client.post(f'/api/sessions/{sid}/start',json=dict(key='start-api-paper',version=1)).status_code==200
        result=wait(client,lambda s:len(s.get('paper',{}).get('orders',[]))==2)
        assert result['mode']=='PAPER_SIMULATION'
        assert not result['execution_connected']
        for _ in range(20):
            assert len(client.get('/api/status').json()['paper']['orders'])==2
        end=client.post(f'/api/sessions/{sid}/end',json=dict(key='end-api-paper',version=result['active']['version']))
        assert end.status_code==200 and end.json()['state']=='DRAINING'
        wait(client,lambda s:s['active'] is None)
        report=client.get(f'/api/sessions/{sid}/report').json()
        assert report['paper']['flat']
        assert report['paper']['reconciliation']['complete']
        assert report['status']=='PAPER_SIMULATION_REPORT'
        assert report['realized_pnl_usd'] is None
        assert report['paper']['actual_exchange_orders_sent']==0
        assert client.post('/api/send',json={}).status_code==404


def test_schema_v1_migration_backs_up_and_preserves_existing_rules_and_report(tmp_path):
    path=tmp_path/'sessions.sqlite'
    store=SessionStore(path)
    s=store.command('create','legacy-create')
    store.command('end','legacy-end-1',sid=s['id'],version=1)
    report=store.report(s['id'])
    for name in ('paper_orders','paper_fills','paper_status','paper_signals','account_evidence'):
        store.db.execute('DROP TABLE '+name)
    store.db.execute('PRAGMA user_version=1');store.close()
    store=SessionStore(path)
    try:
        assert store.db.execute('PRAGMA user_version').fetchone()[0]==2
        assert store.report(s['id'])==report
        assert len(list(tmp_path.glob('*.pre-v2-*.sqlite')))==1
    finally:
        store.close()


def test_entropy_only_session_cannot_start_paper_execution(tmp_path):
    from test_research import save_catalog,metadata
    from entropy_arb.watchlist import resolve_catalog
    root=tmp_path/'collector'
    save_catalog(root,resolve_catalog(*metadata()))
    with TestClient(create_app(state_dir=tmp_path/'state',data_root=root,paper_feed=PublicFeedFixture())) as client:
        s=client.post('/api/sessions',json=dict(key='create-ionq',rules=dict(symbol='IONQ',run_mode='PAPER'))).json()
        assert client.post(f'/api/sessions/{s["id"]}/start',json=dict(key='start-ionq',version=1)).status_code==409
        assert client.get('/api/status').json()['active']['state']=='DRAFT'
