from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import pytest

from entropy_arb.workbench import SessionConflict, SessionStore
from web.workbench import create_app


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
