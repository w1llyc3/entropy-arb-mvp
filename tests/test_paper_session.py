import copy
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import time

import pytest

from entropy_arb.book import OrderBook
from entropy_arb.paper_session import PaperSession
from entropy_arb.workbench import SessionStore, SessionConflict


def book(bid, ask, depth=100):
    result=OrderBook()
    result.apply_hl([[dict(px=str(bid),sz=str(depth))],[dict(px=str(ask),sz=str(depth))]])
    return result


def books(close=False):
    return dict(entropy=book(100.5,100.51) if close else book(100,100.01),
                lighter=book(100.5,100.51) if close else book(101,101.01))


def setup(tmp_path, **rules):
    store=SessionStore(tmp_path/'sessions.sqlite')
    spec=dict(run_mode="PAPER", fee_entropy_bps=0,fee_lighter_bps=0,slippage_bps=0,
              funding_buffer_bps=0,max_loss_usd=25)
    spec.update(rules)
    session=store.command("create","create-paper",rules=spec)
    sid=session["id"]
    store.command("start","start-paper",sid=sid,version=1)
    return store,PaperSession(store),sid


def test_roundtrip_depth_simulation_inventory_fees_and_final_report(tmp_path):
    store,paper,sid=setup(tmp_path,fee_entropy_bps=.9)
    try:
        market=books()
        paper.tick(sid,market)
        assert len(paper.orders(sid))==2
        assert paper.positions(sid)["entropy"]>0 and paper.positions(sid)["lighter"]<0
        assert sum(paper.positions(sid).values())==0
        assert paper.projection(sid)["realized_pnl_before_funding_usd"] is None
        assert all(o["state"]=="FILLED" for o in paper.orders(sid))
        paper.tick(sid,books(True))
        assert paper.can_finish(sid)
        assert len(paper.orders(sid))==4
        result=paper.projection(sid)
        assert D(result["fees_usd"])>0
        assert D(result["realized_pnl_before_funding_usd"])>0
        assert result["fill_edge_distribution"] and result["actual_exchange_orders_sent"]==0
        current=store.snapshot()["active"]
        store.command("end","end-paper-1",sid=sid,version=current["version"])
        report=store.report(sid)
        assert report["status"]=="PAPER_SIMULATION_REPORT"
        assert report["actual_inventory"] is None and not report["settlement_verified"]
        assert report["paper"]["flat"]
        assert report["session"]["rules_hash"]
    finally:
        store.close()


def test_same_snapshot_concurrent_ticks_are_idempotent_and_inventory_cap_holds(tmp_path):
    store,paper,sid=setup(tmp_path)
    try:
        market=books()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _:paper.tick(sid,market),range(20)))
        assert len(paper.orders(sid))==2
        paper.tick(sid,books())
        assert len(paper.orders(sid))==4
        paper.tick(sid,books())
        assert len(paper.orders(sid))==4
        assert paper.projection(sid)["status"]=="RISK_BLOCKED"
    finally:
        store.close()


@pytest.mark.parametrize("bad",["stale","skew","crossed","nan","missing"])
def test_unusable_depth_never_generates_orders(tmp_path,bad):
    store,paper,sid=setup(tmp_path)
    try:
        market=books()
        if bad=="stale": market["entropy"].last_update_ts=time.time()-60
        if bad=="skew": market["entropy"].last_update_ts=time.time()-3
        if bad=="crossed": market["entropy"]=book(101,100)
        if bad=="nan": market["entropy"].asks={float('nan'):1}
        if bad=="missing": del market["lighter"]
        paper.tick(sid,market)
        assert paper.orders(sid)==[]
        assert paper.projection(sid)["status"]=="WAITING_FOR_FRESH_BOOKS"
    finally:
        store.close()


def test_costs_and_worst_leg_risk_block_entries(tmp_path):
    store,paper,sid=setup(tmp_path,fee_entropy_bps=60)
    try:
        paper.tick(sid,books())
        assert paper.orders(sid)==[]
        assert D(paper.projection(sid)["signal"]["entry_net_bps"])<0
    finally:
        store.close()
    other=tmp_path/'small';other.mkdir()
    store,paper,sid=setup(other,max_unhedged_usd=1)
    try:
        paper.tick(sid,books())
        assert paper.orders(sid)==[]
        assert paper.projection(sid)["status"]=="RISK_BLOCKED"
    finally:
        store.close()


def test_pause_keeps_exit_management_and_end_waits_for_fresh_reduction(tmp_path):
    store,paper,sid=setup(tmp_path)
    try:
        paper.tick(sid,books())
        store.command("pause","pause-paper",sid=sid,version=2)
        paper.tick(sid,books())
        assert len(paper.orders(sid))==2
        store.command("end","drain-paper",sid=sid,version=3)
        assert store.snapshot()["active"]["state"]=="DRAINING"
        paper.tick(sid,{})
        assert store.snapshot()["active"]["state"]=="DRAINING"
        paper.tick(sid,books())
        assert store.snapshot()["active"] is None
        assert store.report(sid)["paper"]["flat"]
    finally:
        store.close()


def test_lost_ack_restarts_query_no_resend_and_confirms_exact_fills(tmp_path):
    store,paper,sid=setup(tmp_path)
    paper.faults={"entropy":"lose_ack"}
    paper.tick(sid,books())
    ids={o["id"] for o in paper.orders(sid)}
    assert paper.projection(sid)["positions"]["entropy"] is None
    assert store.snapshot()["active"]["state"]=="RECOVERY_REQUIRED"
    store.close()
    store=SessionStore(tmp_path/'sessions.sqlite');paper=PaperSession(store)
    try:
        store.recover()
        paper.tick(sid,books())
        assert {o["id"] for o in paper.orders(sid)}==ids
        current=store.snapshot()["active"]
        with pytest.raises(SessionConflict,match="query"):
            store.command("resume","unsafe-resume",sid=sid,version=current["version"])
        store.command("reconcile-paper","query-paper-1",sid=sid,version=current["version"])
        store.command("reconcile-paper","query-paper-1",sid=sid,version=current["version"])
        assert not paper.projection(sid)["recovery_required"]
        assert len(paper.fills(sid))==2
        current=store.snapshot()["active"]
        store.command("resume","safe-resume-1",sid=sid,version=current["version"])
        paper.tick(sid,books(True))
        assert paper.can_finish(sid) and len(paper.orders(sid))==4
    finally:
        store.close()


def test_partial_leg_reduces_known_inventory_without_compensating_open(tmp_path):
    store,paper,sid=setup(tmp_path)
    try:
        paper.faults={"entropy":"partial"}
        paper.tick(sid,books())
        assert sum(paper.positions(sid).values())!=0
        assert any(o["state"]=="CANCELED" for o in paper.orders(sid))
        paper.faults={}
        paper.tick(sid,books())
        assert paper.can_finish(sid)
        assert all(o["reduce_only"] for o in paper.orders(sid)[2:])
    finally:
        store.close()


def test_crash_after_claim_is_unknown_not_failed_and_never_resent(tmp_path):
    store,paper,sid=setup(tmp_path)
    def crash(stage,value):
        if stage=="after_claim": raise RuntimeError("simulated crash")
    paper.hook=crash
    with pytest.raises(RuntimeError): paper.tick(sid,books())
    store.close()
    store=SessionStore(tmp_path/'sessions.sqlite');paper=PaperSession(store)
    try:
        store.recover();paper.reconcile(sid)
        assert paper.projection(sid)["recovery_required"]
        assert len(paper.orders(sid))==2
        assert any(o["state"]=="NOT_SENT" for o in paper.orders(sid))
        assert not paper.can_finish(sid)
    finally:
        store.close()


def test_pause_between_intent_and_send_prevents_open(tmp_path):
    store,paper,sid=setup(tmp_path)
    try:
        def pause(stage,value):
            if stage=="after_prepare": store.command("pause","pause-before-send",sid=sid,version=2)
        paper.hook=pause;paper.tick(sid,books())
        assert paper.fills(sid)==[]
        assert all(o["state"]=="NOT_SENT" for o in paper.orders(sid))
    finally:
        store.close()


def test_loss_limit_reduces_and_blocks_reentry(tmp_path):
    store,paper,sid=setup(tmp_path,max_loss_usd=.1)
    try:
        paper.tick(sid,books())
        adverse=dict(entropy=book(90,90.01),lighter=book(101,101.01))
        paper.tick(sid,adverse)
        assert paper.can_finish(sid)
        assert store.snapshot()["active"]["state"]=="PAUSED"
        count=len(paper.orders(sid));paper.tick(sid,books())
        assert len(paper.orders(sid))==count
    finally:
        store.close()


def snapshot(stamp,qty="0",domain="public-domain"):
    return dict(symbol="SNDK",account_domain=domain,legs={v:dict(status="FRESH",observed_at=stamp,
        position_qty=qty,open_order_count=0 if v=="entropy" else None) for v in ("entropy","lighter")})


def test_real_baseline_never_adopts_external_inventory_or_cross_account_delta(tmp_path):
    store,paper,sid=setup(tmp_path)
    try:
        stamp=store._session(sid)["started"]+1
        store.capture_real_evidence(sid,snapshot(stamp,"3"))
        store.capture_real_evidence(sid,snapshot(stamp+1,"4"))
        result=store.real_evidence(sid)
        assert result["status"]=="UNATTRIBUTED_CHANGE"
        assert result["account_position_delta"]["entropy"]=="1"
        assert result["session_owned_inventory"] is None
        assert paper.positions(sid)==dict(entropy=0,lighter=0)
        store.capture_real_evidence(sid,snapshot(stamp+2,"99",domain="different-account"))
        result=store.real_evidence(sid)
        assert result["status"]=="ACCOUNT_DOMAIN_CHANGED"
        assert all(v is None for v in result["account_position_delta"].values())
        assert not result["complete"]
    finally:
        store.close()


def test_paper_duration_enters_draining_not_unmanaged_pause(tmp_path):
    store,paper,sid=setup(tmp_path,duration_minutes=1)
    try:
        started=store._session(sid)['started']
        store.now=lambda:started+61
        store.expire()
        assert store.snapshot()['active']['state']=='DRAINING'
    finally:
        store.close()
