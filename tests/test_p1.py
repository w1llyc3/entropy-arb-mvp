"""Secret-free regressions for the default route and platform control."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from web import live_exec
from web.recorder_ctl import RecorderControl


@pytest.mark.parametrize("kind", ["open", "close", "legs", "error"])
def test_default_route_uses_request_caps_and_closes(monkeypatch, tmp_path, kind):
    cfg = SimpleNamespace(entropy=SimpleNamespace(), hedge=SimpleNamespace(),
                          min_order_notional=10, creds_complete=True,
                          staleness_sec=10)
    monkeypatch.setattr(live_exec, "missing_live_env", lambda root: [])
    monkeypatch.setattr(live_exec, "load_config", lambda *a, **k: cfg)
    seen = []

    class Venue:
        size_decimals = 4
        min_base = .0001
        min_quote = 10
        book = SimpleNamespace(is_fresh=lambda age: True)

        async def load_market(self): pass
        def init_signer(self): seen.append("fake signer")
        def start_tasks(self, *args): return []
        def ready_to_trade(self): return True
        async def close(self): seen.append("venue closed")

    class Session:
        async def close(self): seen.append("session closed")

    class Engine:
        def __init__(self, cfg, record_only=False):
            self.cfg = cfg
            self.stop = asyncio.Event()
            self._update_evt = asyncio.Event()
        def _make_venue(self, cfg): return Venue()
        def request_stop(self): self.stop.set()
        async def execute_confirmed(self, **kw):
            seen.append(kw)
            if kind == "error": raise ConnectionError("fake failure")
            return {"sent": False, "status": "fake-only"}
        async def execute_reduce_legs(self, **kw):
            seen.append(kw)
            return {"sent": False, "status": "fake-only"}

    monkeypatch.setattr(live_exec, "Engine", Engine)
    monkeypatch.setattr(live_exec.aiohttp, "ClientSession", lambda **kw: Session())
    monkeypatch.setattr(live_exec.aiohttp, "TCPConnector", lambda **kw: None)
    req = {"root": str(tmp_path),
        "confirm_id": "fake-id", "direction": "sell_entropy",
        "order_notional_usd": 11, "max_position_usd": 25}
    if kind == "close": req.update(reduce_only=True, close_qty=.005)
    if kind == "legs": req.update(reduce_only=True, flatten_kind="legs",
                                  flatten_legs=[{"venue": "entropy", "qty": .005}])
    if kind == "error":
        with pytest.raises(ConnectionError):
            live_exec.execute_admitted(req, allow_legacy_live=True)
    else:
        result = live_exec.execute_admitted(req, allow_legacy_live=True)
        assert result["status"] == "fake-only"
    assert cfg.max_order_notional == 11
    assert cfg.entropy.cap_usd == cfg.hedge.cap_usd == 25
    assert seen[-3:] == ["venue closed", "venue closed", "session closed"]
    assert next(x for x in seen if isinstance(x, dict))["confirm_id"] == "fake-id"


def test_windows_liveness_never_uses_kill(monkeypatch):
    import web.recorder_ctl as rc
    monkeypatch.setattr(rc.os, "kill", lambda *a: pytest.fail("kill is not a probe"))
    monkeypatch.setattr(rc.sys, "platform", "win32")
    assert RecorderControl._pid_exists(2_000_000_000) is False


def test_pause_resume_are_cooperative(monkeypatch, tmp_path):
    ctl = RecorderControl(tmp_path)
    ctl.dir.mkdir()
    ctl._write_meta({"pid": 123, "argv": []})
    monkeypatch.setattr(ctl, "_alive", lambda meta: True)
    monkeypatch.setattr(ctl, "_signal", lambda *a: pytest.fail("must not freeze feeds"))
    assert ctl.pause()["paused"]
    assert (ctl.dir / "recorder.pause").exists()
    assert not ctl.resume()["paused"]
    assert not (ctl.dir / "recorder.pause").exists()


def test_cli_defaults_record_only(monkeypatch):
    import main
    cfg = SimpleNamespace(dashboard=False, log_level="INFO")
    monkeypatch.setattr(main.sys, "argv", ["main.py", "--symbol", "SNDK",
                                          "--hedge", "lighter", "--no-dashboard"])
    def load(*a, **kw):
        assert a[1] is None  # Default record-only must not read .env.
        return cfg
    monkeypatch.setattr(main, "load_config", load)
    monkeypatch.setattr(main, "setup_logging", lambda *a, **kw: None)
    async def run(*a, **kw): assert kw["record_only"] is True
    monkeypatch.setattr(main, "run_service", run)
    main.main()


def test_default_web_execution_does_not_read_credentials(monkeypatch):
    monkeypatch.setattr(live_exec, "missing_live_env",
                        lambda *a: pytest.fail("credentials must not be read"))
    result = live_exec.execute_admitted({"confirm_id": "safe"})
    assert not result["sent"] and "RECORD_ONLY" in result["error"]


def test_record_only_config_never_reads_secret_sources(monkeypatch):
    import entropy_arb.config as config
    def forbidden(*a, **kw): pytest.fail("secret source accessed")
    monkeypatch.setattr(config, "load_dotenv", forbidden)
    monkeypatch.setattr(config, "_env_s", forbidden)
    monkeypatch.setattr(config, "_env_i", forbidden)
    cfg = config.load_config(str(Path(__file__).parents[1] / "config.yaml"),
                             None, symbol="SNDK", hedge_venue="lighter")
    assert not cfg.creds_complete


def test_pid_reuse_does_not_adopt_process(monkeypatch, tmp_path):
    import web.recorder_ctl as rc
    ctl = RecorderControl(tmp_path)
    monkeypatch.setattr(rc.sys, "platform", "win32")
    monkeypatch.setattr(ctl, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(ctl, "_identity", lambda pid: "new-process")
    assert not ctl._alive({"pid": 123, "creation_id": "old-process"})
    with pytest.raises(rc.RecorderError): ctl._alive({"pid": 123})


def test_recorder_cooperative_pause_skips_sampling(tmp_path):
    from entropy_arb.book import OrderBook
    from entropy_arb.recorder import MinuteRecorder
    b = OrderBook()
    b.apply_hl([[{"px": "100", "sz": "10"}], [{"px": "101", "sz": "10"}]])
    flag = tmp_path / "pause"
    recorder = MinuteRecorder(str(tmp_path / "minutes.csv"), b, b, 10, pause_file=flag)
    flag.touch()
    recorder.sample(120)
    assert recorder._agg is None
    flag.unlink()
    recorder.sample(180)
    assert recorder._agg.n == 1
    flag.touch()
    recorder.sample(181)
    flag.unlink()
    recorder.sample(182)
    assert recorder.rows_written == 0  # Same-minute resume must not create duplicates.
    assert recorder._agg.n == 2
    recorder.close()


def test_stop_marker_requests_engine_shutdown_without_orders(tmp_path):
    from entropy_arb.engine import Engine
    async def run():
        engine = Engine(SimpleNamespace())
        assert engine.record_only
        engine.record_control_dir = str(tmp_path)
        (tmp_path / "recorder.stop").touch()
        await engine._record_control_loop()
        assert engine.stop.is_set()
    asyncio.run(run())
