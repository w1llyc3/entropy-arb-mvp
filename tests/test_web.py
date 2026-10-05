"""Localhost record-only panel: process control, status, analyze gates.

Run:  python3 -m pytest tests/test_web.py
Web routes need requirements-web.txt; they skip when fastapi is absent.
"""
import csv
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.recorder_ctl import (  # noqa: E402
    RecorderControl, RecorderError, record_only_argv)
from web.report import (  # noqa: E402
    ANALYZE_COMMAND, assemble_status, parse_analyze, read_minutes,
    recorder_csv_rel, run_analyze, sample_warnings)

ROOT = os.path.join(os.path.dirname(__file__), "..")

HEADER = [
    "minute_ts", "time_utc", "entropy_bid", "entropy_ask",
    "hedge_bid", "hedge_ask", "premium_open_bps", "premium_high_bps",
    "premium_low_bps", "premium_close_bps", "premium_mean_bps",
    "premium_std_bps", "sell_edge_mean_bps", "sell_edge_max_bps",
    "buy_edge_mean_bps", "buy_edge_max_bps", "samples",
    "fill_sell_edge_100_bps", "fill_buy_edge_100_bps",
    "slip_sell_100_bps", "slip_buy_100_bps", "depth_ok_frac",
]


def _sleep_builder(symbol, hedge):
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def _write_minutes(directory, rows):
    path = directory / "logs"
    path.mkdir(parents=True, exist_ok=True)
    csv_path = path / "minutes.csv"
    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=HEADER, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return csv_path


def _minute(ts, samples=60, prem="1.0", sell="2.5", buy="-0.5",
            fill_sell="8", fill_buy="2", slip_sell="1", slip_buy="3",
            depth="1"):
    row = {k: "" for k in HEADER}
    row.update({
        "minute_ts": ts,
        "time_utc": "2026-10-05T00:00:00Z",
        "entropy_bid": "100.0",
        "entropy_ask": "100.2",
        "hedge_bid": "99.8",
        "hedge_ask": "100.0",
        "premium_close_bps": prem,
        "premium_mean_bps": prem,
        "sell_edge_mean_bps": sell,
        "sell_edge_max_bps": sell,
        "buy_edge_mean_bps": buy,
        "buy_edge_max_bps": buy,
        "samples": samples,
        "fill_sell_edge_100_bps": fill_sell,
        "fill_buy_edge_100_bps": fill_buy,
        "slip_sell_100_bps": slip_sell,
        "slip_buy_100_bps": slip_buy,
        "depth_ok_frac": depth,
    })
    return row


def _linked_repo(tmp_path):
    """A temp project whose analyze.py is this repo's, with its own logs/."""
    root = tmp_path / "repo"
    (root / "tools").mkdir(parents=True)
    (root / "logs").mkdir()
    os.symlink(os.path.join(ROOT, "tools", "analyze.py"),
               root / "tools" / "analyze.py")
    os.symlink(os.path.join(ROOT, "entropy_arb"), root / "entropy_arb")
    os.symlink(os.path.join(ROOT, "config.yaml"), root / "config.yaml")
    return root


def test_record_only_argv_cannot_trade():
    argv = record_only_argv("SNDK", "lighter")
    assert argv[1:] == [
        "main.py", "--record-only", "--no-dashboard",
        "--symbol", "SNDK", "--hedge", "lighter",
    ]
    assert "--record-only" in argv
    joined = " ".join(argv)
    assert ".env" not in joined
    assert "trade" not in joined


def test_csv_path_stays_inside_the_repo(tmp_path):
    assert recorder_csv_rel(tmp_path) == "logs/minutes.csv"
    (tmp_path / "config.yaml").write_text(
        "recorder:\n  csv: /tmp/minutes.csv\n")
    assert recorder_csv_rel(tmp_path) == "logs/minutes.csv"
    (tmp_path / "config.yaml").write_text(
        "recorder:\n  csv: ../minutes.csv\n")
    assert recorder_csv_rel(tmp_path) == "logs/minutes.csv"


def test_status_reads_latest_tob_and_warns_on_thin_samples(tmp_path):
    now = 1_800_000_000.0
    _write_minutes(tmp_path, [
        _minute(now - 120, samples=60, fill_sell="8.5", fill_buy="1.25"),
        _minute(now - 60, samples=4, fill_sell="", fill_buy="3"),
    ])
    status = assemble_status(tmp_path, {"running": False, "pid": None,
                                        "warnings": []}, now=now)
    assert status["minutes_collected"] == 2
    assert status["csv_path"] == "logs/minutes.csv"
    assert "logs/minutes.csv" in status["csv_hint"]
    assert status["samples_coverage"]["latest_samples"] == 4
    assert status["latest"]["tob"]["entropy_bid"] == "100.0"
    assert status["latest"]["tob"]["hedge_ask"] == "100.0"
    assert status["latest"]["fillable_100"]["sell_bps"] is None
    assert status["latest"]["fillable_100"]["buy_bps"] == "3"
    assert any("recent samples dropped" in w and "4/60" in w
               for w in status["warnings"])


def test_sample_warnings_cover_gap_and_stale_recorder():
    now = 1_800_000_000.0
    rows = [
        {"minute_ts": now - 160, "samples": 60},
        {"minute_ts": now - 100, "samples": 50},
    ]
    assert sample_warnings(rows, running=False, uptime_sec=None, now=now) == []
    gap = [
        {"minute_ts": now - 500, "samples": 60},
        {"minute_ts": now - 100, "samples": 60},
    ]
    msgs = sample_warnings(gap, running=False, uptime_sec=None, now=now)
    assert any("gap" in m for m in msgs)
    stale = sample_warnings(
        [{"minute_ts": now - 400, "samples": 60}],
        running=True, uptime_sec=400, now=now)
    assert any("old" in m for m in stale)
    empty = sample_warnings([], running=True, uptime_sec=400, now=now)
    assert any("no minute rows" in m for m in empty)


def test_dead_pid_warns_and_stop_clears_it(tmp_path):
    ctl = RecorderControl(tmp_path)
    pid = 2_000_000_000
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        except OSError:
            break
        pid += 1
    ctl.dir.mkdir(parents=True)
    ctl.pid_path.write_text(f"{pid}\n")
    ctl.meta_path.write_text(
        '{"pid": %d, "started_at": 1, "symbol": "SNDK", "hedge": "lighter",'
        ' "argv": ["python3", "main.py", "--record-only"]}\n' % pid)
    snap = ctl.snapshot()
    assert snap["running"] is False
    assert snap["pid"] == pid
    assert any("dead" in w for w in snap["warnings"])
    stopped = ctl.stop()
    assert stopped["stopped"] is True
    assert not ctl.pid_path.exists()
    assert not ctl.meta_path.exists()


def test_start_stop_cleans_up_the_subprocess(tmp_path):
    ctl = RecorderControl(tmp_path, command_builder=_sleep_builder)
    info = ctl.start("SNDK", "lighter")
    pid = info["pid"]
    try:
        assert (tmp_path / ".web" / "recorder.pid").read_text().strip() == str(pid)
        snap = ctl.snapshot()
        assert snap["running"] is True
        assert snap["pid"] == pid
        assert snap["uptime_sec"] >= 0
        assert snap["symbol"] == "SNDK"
        assert snap["warnings"] == []
        with pytest.raises(RecorderError) as raised:
            ctl.start("SNDK", "lighter")
        assert raised.value.status_code == 409
    finally:
        ctl.stop()
    assert not (tmp_path / ".web" / "recorder.pid").exists()
    assert not (tmp_path / ".web" / "recorder.json").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # a second stop is a no-op cleanup
    assert ctl.stop()["running"] is False


def test_analyze_labels_g1_through_g4(tmp_path):
    root = _linked_repo(tmp_path)
    base = int(time.time()) // 60 * 60
    _write_minutes(root, [
        _minute(base - 180, prem="0", sell="10", buy="4",
                fill_sell="8", fill_buy="2",
                slip_sell="1", slip_buy="3", depth="1"),
        _minute(base - 120, prem="0", sell="20", buy="6",
                fill_sell="12", fill_buy="4",
                slip_sell="2", slip_buy="4", depth="0.5"),
        _minute(base - 60, prem="0", sell="30", buy="8",
                fill_sell="", fill_buy="6",
                slip_sell="", slip_buy="5", depth="0"),
    ])
    result = run_analyze(root)
    assert result["command"] == ANALYZE_COMMAND
    assert result["exit_code"] == 0
    assert result["ok"] is True
    assert result["csv_path"] == "logs/minutes.csv"
    gates = result["gates"]
    assert gates["G1"]["label"].startswith("SELL entropy GATE")
    assert gates["G2"]["label"].startswith("BUY entropy GATE")
    assert gates["G1"]["text"] == "+9.10"
    assert gates["G2"]["text"] == "+3.10"
    assert gates["G3"]["label"] == "slip@$100 p90"
    assert gates["G3"]["sell_text"] == "+1.90"
    assert gates["G3"]["buy_text"] == "+4.80"
    assert gates["G4"]["text"] == "0.5000"
    assert result["slip_p90"]["sell_text"] == "+1.90"
    assert result["slip_p90"]["buy_text"] == "+4.80"
    assert result["depth_ok_frac_text"] == "0.5000"
    assert "G3" in result["stdout"] and "G4" in result["stdout"]
    # the firing table also mentions SELL entropy; it must not replace G1
    again = parse_analyze(result["stdout"])
    assert again["gates"]["G1"]["text"] == "+9.10"


def test_panel_pages_and_routes(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from web.panel import create_app

    app = create_app(tmp_path)
    paths = {getattr(route, "path", None) for route in app.routes}
    assert paths == {"/", "/api/status", "/api/start", "/api/stop", "/api/analyze"}
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        html = page.text
        assert "Start record-only" in html
        assert "logs/minutes.csv" in html
        for label in ("G1", "G2", "G3", "G4", "slip p90", "depth_ok_frac"):
            assert label in html
        assert "Start live" not in html
        assert 'type="password"' not in html
        assert ".env" not in html
        assert "api_key" not in html
        status = client.get("/api/status").json()
        assert status["running"] is False
        assert status["minutes_collected"] == 0
        assert status["csv_path"] == "logs/minutes.csv"
        bad = client.post("/api/start", json={"symbol": "SNDK", "hedge": "binance"})
        assert bad.status_code == 400
        flagged = client.post("/api/start", json={
            "symbol": "../etc", "hedge": "lighter"})
        assert flagged.status_code == 400


def test_http_start_normalizes_inputs_and_stop_cleans_up(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from web.panel import create_app

    app = create_app(tmp_path, command_builder=_sleep_builder)
    pid = None
    with TestClient(app) as client:
        try:
            res = client.post("/api/start", json={
                "symbol": "sndk",
                "hedge": "Lighter",
                "record_only": False,
                "env_file": ".env",
            })
            assert res.status_code == 200, res.text
            body = res.json()
            assert body["symbol"] == "SNDK"
            assert body["hedge"] == "lighter"
            # The stand-in command is a sleep; the request cannot drop
            # --record-only because the route never forwards that field.
            assert body["argv"][1:3] == ["-c", "import time; time.sleep(60)"]
            pid = body["pid"]
            assert client.get("/api/status").json()["running"] is True
            again = client.post("/api/start", json={
                "symbol": "SNDK", "hedge": "lighter"})
            assert again.status_code == 409
        finally:
            stopped = client.post("/api/stop")
            assert stopped.status_code == 200
            assert stopped.json()["stopped"] is True
        assert not (tmp_path / ".web" / "recorder.pid").exists()
        after = client.get("/api/status").json()
        assert after["running"] is False
        assert after["pid"] is None
        assert after["warnings"] == []
    if pid is not None:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_http_start_uses_the_record_only_command(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from web.panel import create_app

    app = create_app(tmp_path)
    captured = {}

    def fake_start(symbol, hedge):
        from web.recorder_ctl import record_only_argv
        argv = record_only_argv(symbol, hedge)
        captured["argv"] = argv
        return {"running": True, "pid": None, "symbol": symbol,
                "hedge": hedge, "argv": argv}

    monkeypatch.setattr(app.state.ctl, "start", fake_start)
    with TestClient(app) as client:
        res = client.post("/api/start", json={
            "symbol": "sndk", "hedge": "lighter-rh",
            "record_only": False, "env_file": ".env",
        })
    assert res.status_code == 200
    argv = captured["argv"]
    assert "--record-only" in argv
    assert argv[argv.index("--symbol") + 1] == "SNDK"
    assert argv[argv.index("--hedge") + 1] == "lighter-rh"
    assert ".env" not in argv
    assert "False" not in argv


def test_http_analyze_shows_labeled_gates(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from web.panel import create_app

    root = _linked_repo(tmp_path)
    app = create_app(root)
    base = int(time.time()) // 60 * 60
    with TestClient(app) as client:
        gone = client.post("/api/analyze").json()
        assert gone["ok"] is False
        assert gone["exit_code"] == 1
        assert "not found" in gone["stderr"].lower()
        _write_minutes(root, [
            _minute(base - 120, fill_sell="8", fill_buy="2",
                    slip_sell="1", slip_buy="3", depth="1"),
            _minute(base - 60, fill_sell="12", fill_buy="4",
                    slip_sell="2", slip_buy="4", depth="0.5"),
        ])
        body = client.post("/api/analyze").json()
    assert body["ok"] is True
    assert body["command"] == ANALYZE_COMMAND
    assert body["gates"]["G1"]["text"] == "+9.10"
    assert body["gates"]["G2"]["text"] == "+2.10"
    assert body["gates"]["G4"]["text"] == "0.7500"
    assert body["slip_p90"]["sell_text"] == "+1.90"
    assert body["depth_ok_frac_text"] == "0.7500"
    assert "slip" in body["gates"]["G3"]["label"]


def test_main_binds_loopback_only(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")
    import uvicorn
    from web.__main__ import HOST, main

    called = {}

    def fake_run(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert HOST == "127.0.0.1"
    main(["--port", "8766"])
    assert called["kwargs"]["host"] == "127.0.0.1"
    assert called["kwargs"]["port"] == 8766
    source = open(os.path.join(ROOT, "web", "__main__.py"),
                  encoding="utf-8").read()
    assert 'add_argument("--host"' not in source
    assert "host=HOST" in source
