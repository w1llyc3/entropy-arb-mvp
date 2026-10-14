import asyncio
from dataclasses import replace
from decimal import Decimal as D
import time

import pytest

from entropy_arb.book import OrderBook
from entropy_arb.service import TradingService,ServiceError
from entropy_arb.risk import RiskRejected
from fakes.venues import FakeVenue


def book(bid,ask):
    b=OrderBook()
    b.apply_hl([[dict(px=str(bid),sz="10")],[dict(px=str(ask),sz="10")]])
    b.funding=0; b.funding_period_sec=3600; b.funding_observed_ts=time.time()
    return b


def setup(root,**kw):
    venues=kw.pop("venues",None) or dict(entropy=FakeVenue(venue="entropy",account="fake-e"),
                                        lighter=FakeVenue(venue="lighter",account="fake-l"))
    books=kw.pop("books",None) or dict(entropy=book(1601,1601.1),lighter=book(1599,1599.1))
    svc=TradingService(root,simulation=True,venues=venues,books=books,interval=.01,
                       reconcile_interval=.1,persist_sec=.02,lock_dir=root/"locks",in_rth=lambda:True,**kw)
    return svc,venues,books


async def wait_for(predicate,timeout=2):
    deadline=time.monotonic()+timeout
    while not predicate():
        if time.monotonic()>deadline: raise AssertionError("service condition timeout")
        await asyncio.sleep(.01)


async def armed(svc,auto=False):
    await svc.call("task",dict(mode="record",midline_bps=0,upper_bps=2,lower_bps=2,
                              auto_confirm=auto,rth_only=False))
    await svc.call("start")


def test_background_manual_idempotency_and_authoritative_settlement(tmp_path):
    async def run():
        svc,venues,books=setup(tmp_path)
        await svc.start()
        try:
            await armed(svc)
            await wait_for(lambda:svc.snapshot().get("proposal"))
            p=svc.snapshot()["proposal"]
            results=await asyncio.gather(*(svc.call("confirm",dict(confirm=True,proposal_id=p["proposal_id"])) for _ in range(20)))
            assert len({r["operation_id"] for r in results})==1
            assert [v.submit_calls for v in venues.values()]==[1,1]
            assert results[0]["operation"]["state"]=="SETTLED"
            assert svc.recovered
            assert all(D(l["reservation"])==0 for l in results[0]["operation"]["legs"])
            assert svc.ledger.metrics()["fills"]==2
            await svc.call("pause")
            assert svc.snapshot()["paused"]
            await svc.call("reconcile")  # Pausing must not stop account reads.
            assert svc.recovered
        finally: await svc.close()
    asyncio.run(run())


def test_auto_open_and_close_runs_without_browser_requests(tmp_path):
    async def run():
        svc,venues,books=setup(tmp_path)
        await svc.start()
        try:
            await armed(svc,True)
            await wait_for(lambda:all(v.submit_calls==1 for v in venues.values()))
            await wait_for(lambda:svc.recovered)
            books["entropy"]=book(1599,1599.1)
            await wait_for(lambda:all(v.submit_calls==2 for v in venues.values()))
            await wait_for(lambda:svc.recovered)
            await svc.call("pause")
            assert all(v.state.position==0 for v in venues.values())
            assert len(svc.ledger.all_operations())==2
        finally: await svc.close()
    asyncio.run(run())


def test_external_net_zero_positions_are_not_adopted(tmp_path):
    async def run():
        svc,venues,_=setup(tmp_path)
        venues["entropy"].state.position=D("-.01")
        venues["lighter"].state.position=D(".01")
        await svc.start()
        try:
            await armed(svc,True)
            await asyncio.sleep(.1)
            assert not svc.recovered
            assert svc.ledger.all_operations()==[]
            assert svc.flatten_offer() is None
            assert [v.submit_calls for v in venues.values()]==[0,0]
        finally: await svc.close()
    asyncio.run(run())


def test_unknown_blocks_new_open_and_recovers_same_ids(tmp_path):
    async def run():
        svc,venues,_=setup(tmp_path)
        for v in venues.values(): v.fault="lose_ack"; v.query_lag=5
        await svc.start()
        try:
            await armed(svc)
            await wait_for(lambda:svc.snapshot().get("proposal"))
            p=svc.snapshot()["proposal"]
            result=await svc.call("confirm",dict(confirm=True,proposal_id=p["proposal_id"]))
            ids=[l["client_id"] for l in result["operation"]["legs"]]
            assert not svc.recovered
            assert svc.ledger.pending_operations()
            for v in venues.values(): v.query_lag=0
            await svc.call("reconcile")
            assert svc.recovered
            assert ids==[l["client_id"] for l in svc.ledger.operation(result["operation_id"])["legs"]]
            assert [v.submit_calls for v in venues.values()]==[1,1]
        finally: await svc.close()
    asyncio.run(run())


def test_restart_preserves_ownership_and_never_resends(tmp_path):
    async def run():
        svc,venues,books=setup(tmp_path)
        await svc.start()
        await armed(svc)
        await wait_for(lambda:svc.snapshot().get("proposal"))
        p=svc.snapshot()["proposal"]
        r=await svc.call("confirm",dict(confirm=True,proposal_id=p["proposal_id"]))
        await svc.close()
        other,_,_=setup(tmp_path,venues=venues,books=books)
        await other.start()
        try:
            assert other.recovered
            assert other.ledger.operation(r["operation_id"])["state"]=="SETTLED"
            assert [v.submit_calls for v in venues.values()]==[1,1]
            assert other.task_spec["mode"]=="record"
        finally: await other.close()
    asyncio.run(run())


def test_single_owner_rejects_second_service(tmp_path):
    async def run():
        first,_,_=setup(tmp_path)
        second,_,_=setup(tmp_path)
        await first.start()
        try:
            with pytest.raises(ServiceError): await second.start()
        finally: await first.close()
        await second.start(); await second.close()
    asyncio.run(run())


def test_stale_or_regressed_snapshots_do_not_release(tmp_path):
    async def run():
        svc,venues,_=setup(tmp_path)
        await svc.start()
        try:
            await armed(svc)
            old=venues["entropy"].fetch_snapshot
            async def stale(): return replace(await old(),observed_at=time.time()-60)
            venues["entropy"].fetch_snapshot=stale
            await svc.call("reconcile")
            assert not svc.recovered
            assert svc.recovery.snapshots["entropy"].complete is False
        finally: await svc.close()
    asyncio.run(run())


def test_partial_fill_requires_explicit_owned_reduction(tmp_path):
    async def run():
        svc,venues,_=setup(tmp_path)
        venues["entropy"].fill_fraction=D(".5")
        await svc.start()
        try:
            await armed(svc)
            await wait_for(lambda:svc.snapshot().get("proposal"))
            p=svc.snapshot()["proposal"]
            await svc.call("confirm",dict(confirm=True,proposal_id=p["proposal_id"]))
            assert not svc.recovered
            assert [v.submit_calls for v in venues.values()]==[1,1]
            assert "residual net" in svc.reason
            venues["entropy"].fill_fraction=D(1)
            offer=svc.flatten_offer()
            payload=dict(confirm=True,proposal_id=offer["proposal_id"],idempotency_key="manual-reduce")
            first=await svc.call("flatten",payload)
            again=await svc.call("flatten",payload)
            assert first["operation_id"]==again["operation_id"]
            assert svc.recovered and all(v.state.position==0 for v in venues.values())
            assert [v.submit_calls for v in venues.values()]==[2,2]
        finally: await svc.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault",["sequence","orders","watermark","nan_watermark","delta","missing_fills"])
def test_inconsistent_evidence_blocks_recovery_and_reduction(tmp_path,fault):
    async def run():
        svc,venues,_=setup(tmp_path)
        await svc.start()
        try:
            await armed(svc)
            await wait_for(lambda:svc.snapshot().get("proposal"))
            p=svc.snapshot()["proposal"]
            await svc.call("confirm",dict(confirm=True,proposal_id=p["proposal_id"]))
            old=venues["entropy"].fetch_snapshot
            async def wrong():
                snapshot=await old()
                changes={"sequence":dict(sequence=-1),"orders":dict(open_orders=("external-order",)),
                         "watermark":dict(settled_through=None),"nan_watermark":dict(settled_through=float("nan")),
                         "delta":dict(position=D("42"))}
                return replace(snapshot,**changes[fault])
            if fault=="missing_fills":
                leg=svc.ledger.all_operations()[0]["legs"][0]
                with svc.ledger.transaction() as db: db.execute("DELETE FROM fills WHERE leg_id=?",(leg["id"],))
                async def no_fills(_): return []
                venues[leg["venue"]].query_fills=no_fills
            else: venues["entropy"].fetch_snapshot=wrong
            result=await svc.call("reconcile")
            assert not result["ok"] and not svc.recovered
            assert svc.flatten_offer() is None
            assert [v.submit_calls for v in venues.values()]==[1,1]
        finally: await svc.close()
    asyncio.run(run())


def test_account_lock_crosses_workspace_boundary(tmp_path):
    async def run():
        venues=dict(entropy=FakeVenue(venue="entropy",account="fake-e"),
                    lighter=FakeVenue(venue="lighter",account="fake-l"))
        kwargs=dict(simulation=True,venues=venues,books={},lock_dir=tmp_path/"shared-locks")
        first=TradingService(tmp_path/"first",**kwargs)
        second=TradingService(tmp_path/"second",**kwargs)
        await first.start()
        try:
            with pytest.raises(ServiceError,match="another workspace"): await second.start()
        finally: await first.close()
        await second.start(); await second.close()
    asyncio.run(run())


def test_legacy_recorder_is_never_implicitly_adopted(tmp_path):
    async def run():
        (tmp_path/".web").mkdir()
        (tmp_path/".web"/"recorder.pid").write_text("unverified legacy metadata")
        svc,venues,_=setup(tmp_path)
        with pytest.raises(ServiceError,match="legacy recorder"): await svc.start()
        assert all(v.submit_calls==0 for v in venues.values())
    asyncio.run(run())


def test_restored_recording_starts_collector_without_browser(tmp_path):
    class Collector:
        task=None
        starts=0
        books={}
        async def start(self):
            self.starts+=1
            self.task=asyncio.create_task(asyncio.Event().wait())
        def pause(self,_): pass
        async def close(self):
            if self.task:
                self.task.cancel(); await asyncio.gather(self.task,return_exceptions=True)
                self.task=None
    async def run():
        collector=Collector()
        svc,venues,books=setup(tmp_path,collector=collector)
        await svc.start(); await armed(svc); await svc.close()
        assert collector.starts==1
        other,_,_=setup(tmp_path,venues=venues,books=books,collector=collector)
        await other.start()
        try:
            await wait_for(lambda:collector.starts==2)
            assert other.recording
        finally: await other.close()
    asyncio.run(run())


def test_collector_close_failure_releases_owner(tmp_path):
    class BrokenCollector:
        async def close(self): raise RuntimeError("fake cleanup failure")
    async def run():
        svc,_,_=setup(tmp_path,collector=BrokenCollector())
        await svc.start()
        with pytest.raises(RuntimeError,match="cleanup"): await svc.close()
        second,_,_=setup(tmp_path)
        await second.start(); await second.close()
    asyncio.run(run())


def test_pre_send_crash_remains_proven_unsent_across_two_restarts(tmp_path):
    async def run():
        from entropy_arb.storage import LegSpec
        svc,venues,books=setup(tmp_path)
        await svc.start()
        specs=[LegSpec(n,svc.accounts[n],D(".01"),D("1600"),n=="entropy") for n in venues]
        op=svc.ledger.create_intent("crash-before-claim","durable-only","open",specs,baselines=svc.recovery.snapshots)
        await svc.close()
        for _ in range(2):
            other,_,_=setup(tmp_path,venues=venues,books=books)
            await other.start()
            try:
                assert other.recovered
                assert other.ledger.operation(op)["state"]=="SETTLED"
                assert all(l["outcome"]=="not_sent" for l in other.ledger.operation(op)["legs"])
                assert all(v.submit_calls==0 for v in venues.values())
            finally: await other.close()
    asyncio.run(run())


def test_non_rth_acknowledgement_survives_status_refresh(tmp_path):
    async def run():
        svc,venues,_=setup(tmp_path)
        svc.in_rth=lambda:False
        await svc.start()
        try:
            await svc.call("task",dict(mode="record",midline_bps=0,upper_bps=2,lower_bps=2,rth_only=True))
            await svc.call("start")
            await wait_for(lambda:svc.snapshot().get("proposal"))
            p=svc.snapshot()["proposal"]
            assert p["force_required"] and not p["force_ack"]
            body=dict(confirm=True,proposal_id=p["proposal_id"])
            assert (await svc.call("confirm",body))["needs_force_confirm"]
            refreshed=svc.snapshot()["proposal"]
            assert refreshed["force_required"] and refreshed["force_ack"]
            assert all(v.submit_calls==0 for v in venues.values())
            body["force_confirm"]=True
            assert (await svc.call("confirm",body))["sent"]
        finally: await svc.close()
    asyncio.run(run())
