from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import pytest

from entropy_arb.workbench import SessionConflict, SessionStore
from web.workbench import create_app
from entropy_arb.watchlist import ASSETS


def test_concurrent_create_replay_and_conflicting_payload(tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite")
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: store.command("create", "same-click"), range(20)))
        assert len({s["id"] for s in results}) == 1
        assert len(store.snapshot()["sessions"]) == 1
        assert len(store.events(results[0]["id"])) == 1
        with pytest.raises(SessionConflict, match="reused"):
            store.command("create", "same-click", rules={"entry_bps": 20})
        with pytest.raises(SessionConflict, match="existing"):
            store.command("create", "second-session")
    finally:
        store.close()


def test_durable_restart_blocks_resume_until_explicit_command_and_preserves_rules(tmp_path):
    path = tmp_path / "sessions.sqlite"
    store = SessionStore(path, now=lambda:100)
    first = store.command("create", "new-session", rules={"entry_bps": 15})
    started = store.command("start", "start-session", sid=first["id"], version=1)
    store.close()
    store = SessionStore(path, now=lambda:110)
    try:
        store.recover()
        recovered = store.snapshot()["active"]
        assert recovered["state"] == "RECOVERY_REQUIRED"
        assert recovered["rules"]["entry_bps"] == 15
        assert not recovered["live_enabled"]
        with pytest.raises(SessionConflict, match="changed"):
            store.command("resume", "stale-resume", sid=first["id"], version=started["version"])
        resumed = store.command("resume", "fresh-resume", sid=first["id"], version=recovered["version"])
        assert resumed["state"] == "OBSERVING"
    finally:
        store.close()


def test_duration_expiry_is_durable_and_cannot_reset_budget_by_resuming(tmp_path):
    clock = [100]
    store = SessionStore(tmp_path / "sessions.sqlite", now=lambda:clock[0])
    try:
        session = store.command("create", "create-key", rules={"duration_minutes":1})
        sid = session["id"]
        store.command("start", "start-key", sid=sid, version=1)
        clock[0] = 160
        store.expire()
        current = store.snapshot()["active"]
        assert current["state"] == "PAUSED"
        store.expire()
        assert len(store.events(sid)) == 3
        with pytest.raises(SessionConflict, match="exhausted"):
            store.command("resume", "resume-key", sid=sid, version=current["version"])
        ended = store.command("end", "end-key-1", sid=sid, version=current["version"])
        assert ended["state"] == "ENDED"
        report = store.report(sid)
        assert report["status"] == "PRELIMINARY_OBSERVATION"
        for field in ("actual_inventory", "realized_pnl_usd", "fees_usd", "own_fill_distribution"):
            assert report[field] is None
        clock[0] = 200
        assert store.report(sid) == report
        assert store.command("end", "end-key-1", sid=sid, version=current["version"]) == ended
        assert store.command("create", "next-session")["state"] == "DRAFT"
    finally:
        store.close()


@pytest.mark.parametrize("rules", [
    {"entry_bps":float("nan")}, {"order_usd":True}, {"entry_bps":2,"exit_bps":3},
    {"max_inventory_usd":26}, {"max_unhedged_usd":12}, {"duration_minutes":0},
    {"secret":"never-accept"}, {"direction":"OTHER"}, {"strategy":"PASSIVE"}, [], "rules",
])
def test_invalid_rules_never_create_session(tmp_path, rules):
    store = SessionStore(tmp_path / "sessions.sqlite")
    try:
        with pytest.raises((ValueError, SessionConflict)):
            store.command("create", "invalid-rules", rules=rules)
        assert store.snapshot()["sessions"] == []
    finally:
        store.close()


def test_independent_api_never_touches_collector_or_credentials(tmp_path, monkeypatch):
    from entropy_arb import live_transport
    from web.recorder_ctl import RecorderControl
    def forbidden(*args, **kwargs):
        pytest.fail("collector control or credential access attempted")
    monkeypatch.setattr(live_transport, "read_operator_env", forbidden)
    for name in ("start", "stop", "pause", "resume"):
        monkeypatch.setattr(RecorderControl, name, forbidden)
    data = tmp_path / "collector"
    data.mkdir()
    (data / ".env").write_text("DO_NOT_READ=true", encoding="utf-8")
    sentinel = data / "recording.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    with TestClient(create_app(state_dir=tmp_path / "separate", data_root=data)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/workbench.js").status_code == 200
        assert client.get("/api/basis").json()["points"] == []
        created = client.post("/api/sessions", json={"key":"api-create", "rules":{}}).json()
        sid = created["id"]
        response = client.post(f"/api/sessions/{sid}/start", json={"key":"api-start", "version":1})
        assert response.json()["state"] == "OBSERVING"
        # GET polling must not append lifecycle events.
        before = client.get("/api/status").json()
        for _ in range(10):
            assert client.get("/api/status").json() == before
        assert before["inventory"] is None
        assert not before["execution_connected"]
        assert client.post("/api/send", json={}).status_code == 404
        assert client.post("/api/flatten", json={}).status_code == 404
        for body in ({"key":"bad-secret", "private_key":"invalid"}, {"key":"bad-live", "live_enabled":True}):
            assert client.post("/api/sessions",json=body).status_code == 400
        assert client.post(f"/api/sessions/{sid}/end", json={"key":"api-end-1", "version":2}).status_code == 200
        report = client.get(f"/api/sessions/{sid}/report").json()
        assert not report["settlement_verified"]
        assert len(report["events"]) == 3
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert sorted(p.name for p in data.iterdir()) == [".env", "recording.txt"]


def test_origin_and_exclusive_process_owner(tmp_path):
    state = tmp_path / "state"
    with TestClient(create_app(state_dir=state, data_root=tmp_path)) as client:
        assert client.get("/api/status",headers={"Host":"evil.example"}).status_code == 403
        assert client.post("/api/sessions", json={"key":"origin-key"},headers={"Origin":"http://evil.example"}).status_code == 403
        with pytest.raises(RuntimeError, match="already owned"):
            with TestClient(create_app(state_dir=state, data_root=tmp_path)):
                pass
        assert client.get("/api/status").json()["sessions"] == []


@pytest.mark.parametrize("asset", ASSETS, ids=lambda a:a.symbol)
def test_watchlist_sessions_charts_and_reports_are_asset_bound(tmp_path, asset):
    with TestClient(create_app(state_dir=tmp_path/"state",data_root=tmp_path/"collector")) as client:
        catalog=client.get("/api/assets").json()
        assert len(catalog["assets"])==10
        assert all(not a["execution_enabled"] for a in catalog["assets"])
        created=client.post("/api/sessions",json=dict(key="create-asset",rules=dict(symbol=asset.symbol))).json()
        assert created["rules"]["symbol"]==asset.symbol
        sid=created["id"]
        assert client.get("/api/basis",params=dict(symbol=asset.symbol)).json()["symbol"]==asset.symbol
        inventory=client.get("/api/inventory",params=dict(symbol=asset.symbol)).json()
        assert inventory["symbol"]==asset.symbol and not inventory["execution_ready"]
        assert all(r["position_qty"] is None for r in inventory["legs"].values())
        # Browsing a different chart cannot retarget a frozen session.
        client.get("/api/basis",params=dict(symbol="TCNT"))
        assert client.get("/api/status").json()["active"]["rules"]["symbol"]==asset.symbol
        assert client.post(f"/api/sessions/{sid}/end",json=dict(key="end-asset",version=1)).status_code==200
        assert client.get(f"/api/sessions/{sid}/report").json()["session"]["rules"]["symbol"]==asset.symbol
        assert client.get("/api/basis?symbol=BAD").status_code==400
        assert client.get("/api/inventory?symbol=BAD").status_code==400
        assert client.post("/api/sessions",json=dict(key="bad-symbol",rules=dict(symbol="BAD"))).status_code==400


def test_legacy_sndk_rules_normalized_without_rewriting_evidence(tmp_path):
    store=SessionStore(tmp_path/"sessions.sqlite")
    try:
        s=store.command("create","old-session")
        rules=s["rules"].copy();del rules["symbol"]
        store.db.execute("UPDATE sessions SET rules=? WHERE id=?",(json.dumps(rules),s["id"]))
        assert store.snapshot()["active"]["rules"]["symbol"]=="SNDK"
        stored=store.db.execute("SELECT rules FROM sessions").fetchone()[0]
        assert "symbol" not in json.loads(stored)
    finally:
        store.close()


def test_chart_data_isolation_and_single_venue_no_fake_basis(tmp_path):
    import time
    from test_research import write_rows, row, save_catalog, metadata
    from entropy_arb.watchlist import resolve_catalog
    root=tmp_path/"collector"
    stamp=int(time.time()//60)*60-120
    write_rows(root,"SNDK",[row(stamp,99)])
    write_rows(root,"ANTH",[row(stamp,3)])
    write_rows(root,"IONQ",[row(stamp,999)],single=True)
    save_catalog(root,resolve_catalog(*metadata()))
    with TestClient(create_app(state_dir=tmp_path/"state",data_root=root)) as client:
        sndk=client.get("/api/basis?symbol=SNDK").json()
        anth=client.get("/api/basis?symbol=ANTH").json()
        single=client.get("/api/basis?symbol=IONQ").json()
        assert sndk["points"][0]["basis_bps"]==99
        assert anth["points"][0]["basis_bps"]==3
        assert not single["paired"]
        assert single["points"][0]["entropy_usd"]==101
        assert single["points"][0]["basis_bps"] is None
        assert single["summary"]["basis_count"]==0


def test_session_asset_cannot_be_retargeted_by_browsing_or_command(tmp_path):
    with TestClient(create_app(state_dir=tmp_path/"state",data_root=tmp_path)) as client:
        s=client.post("/api/sessions",json=dict(key="start-nbis",rules=dict(symbol="NBIS"))).json()
        url=f'/api/sessions/{s["id"]}/start'
        assert client.post(url,json=dict(key="retarget-try",version=1,symbol="SNDK")).status_code==400
        assert client.post(url,json=dict(key="start-valid",version=1)).status_code==200
        assert client.post("/api/sessions",json=dict(key="other-asset",rules=dict(symbol="SNDK"))).status_code==409
        client.get("/api/basis?symbol=SNDK")
        assert client.get("/api/status").json()["active"]["rules"]["symbol"]=="NBIS"
