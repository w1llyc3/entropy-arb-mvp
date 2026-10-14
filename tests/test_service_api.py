import asyncio
import time

from fastapi.testclient import TestClient
import pytest

from entropy_arb.service import TradingService
from web.panel import create_app
from test_service import setup,wait_for


def test_actual_app_gets_are_read_only_and_persisted_commands_are_shared(tmp_path):
    svc,venues,_=setup(tmp_path)
    app=create_app(tmp_path,service=svc)
    with TestClient(app) as client:
        assert not hasattr(app.state,"session")  # No ProbeSession state owner.
        assert client.post("/api/task",json=dict(mode="record",midline_bps=0,upper_bps=2,lower_bps=2)).status_code==200
        assert client.post("/api/session/start").status_code==200
        deadline=time.monotonic()+2
        proposal=None
        while time.monotonic()<deadline:
            proposal=client.get("/api/status").json().get("proposal")
            if proposal: break
            time.sleep(.01)
        assert proposal
        before=[v.submit_calls for v in venues.values()]
        for _ in range(1000):
            assert client.get("/api/status").status_code==200
        assert before==[v.submit_calls for v in venues.values()]==[0,0]
        # Long GET polling may expire the original card. Fetch a current one.
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            proposal=client.get("/api/status").json().get("proposal")
            if proposal: break
        body=dict(confirm=True,proposal_id=proposal["proposal_id"],idempotency_key="http-manual")
        response=client.post("/api/confirm",json=body)
        assert response.status_code==200,response.text
        op=response.json()["operation_id"]
        assert client.post("/api/confirm",json=body).json()["operation_id"]==op
        assert client.get("/api/operations/"+op).json()["state"]=="SETTLED"
        assert [v.submit_calls for v in venues.values()]==[1,1]
        assert client.post("/api/pause").status_code==200
        r=client.post("/api/reconcile").json()
        assert r["job_id"] and r["query_count"]>=6 and r["ok"]
        assert client.get("/health/live").status_code==200
        assert client.get("/health/ready").status_code==503
        assert client.post("/api/stop").json()["stopped"]
    assert svc.worker.done()


def test_production_default_never_reads_env_or_signs(tmp_path,monkeypatch):
    import web.accounts as accounts
    import entropy_arb.config as config
    def forbidden(*a,**kw): pytest.fail("secret source accessed")
    monkeypatch.setattr(accounts,"parse_env_file",forbidden)
    monkeypatch.setattr(config,"load_dotenv",forbidden)
    svc=TradingService(tmp_path,interval=.01,reconcile_interval=1,lock_dir=tmp_path/"locks")
    with TestClient(create_app(tmp_path,service=svc)) as client:
        assert client.get("/").status_code==200
        s=client.get("/api/status").json()
        assert s["mode"]=="RECORD_ONLY"
        assert s["accounts"]["entropy"]["position"] is None
        assert not s["trading_ready"]
        assert client.post("/api/task",json=dict(mode="live")).status_code==409
        assert client.post("/api/confirm",json=dict(confirm=True)).status_code==409
        assert client.post("/api/flatten",json=dict(confirm=True)).status_code==409
        assert client.post("/api/reconcile").json()["ok"] is False


def test_worker_survives_http_client_cancellation(tmp_path):
    async def run():
        svc,venues,_=setup(tmp_path)
        await svc.start()
        try:
            from test_service import armed
            await armed(svc)
            await wait_for(lambda:svc.snapshot().get("proposal"))
            for v in venues.values():
                original=v.submit
                async def slow(intent,submit=original):
                    await asyncio.sleep(.05)
                    return await submit(intent)
                v.submit=slow
            p=svc.snapshot()["proposal"]
            request=asyncio.create_task(svc.call("confirm",dict(confirm=True,proposal_id=p["proposal_id"])))
            await asyncio.sleep(.01); request.cancel()
            await asyncio.gather(request,return_exceptions=True)
            await wait_for(lambda:svc.ledger.metrics()["fills"]==2)
            assert all(v.submit_calls==1 for v in venues.values())
            assert svc.worker and not svc.worker.done()
        finally: await svc.close()
    asyncio.run(run())


def test_cli_restart_does_not_start_an_already_restored_recording(tmp_path,monkeypatch):
    import main
    import entropy_arb.service as service_module
    from types import SimpleNamespace
    commands=[]
    class Restored:
        recording=True
        task_spec={"mode":"record"}
        def __init__(self,*a,**kw): pass
        async def start(self): pass
        async def call(self,name,*a): commands.append(name)
        async def close(self): commands.append("close")
    monkeypatch.setattr(service_module,"TradingService",Restored)
    monkeypatch.setattr(main,"install_stop_handlers",lambda loop,stop:stop())
    asyncio.run(main.run_service(SimpleNamespace(),root=tmp_path,use_dashboard=False))
    assert commands==["stop","close"]
