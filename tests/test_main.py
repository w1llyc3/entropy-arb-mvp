"""Windows startup: signal handlers and diagnosable startup errors.

Run:  python3 -m pytest tests/test_main.py
"""
import asyncio
import os
import signal
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def test_unix_stop_handlers_use_the_loop():
    calls = []

    class Loop:
        def add_signal_handler(self, sig, callback):
            calls.append(sig)

    main.install_stop_handlers(Loop(), lambda: None)
    assert calls == [signal.SIGINT, signal.SIGTERM]


def test_windows_stop_handlers_fall_back(monkeypatch):
    class Loop:
        def add_signal_handler(self, sig, callback):
            raise NotImplementedError

    registered = {}

    def fake_signal(sig, handler):
        registered[sig] = handler
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)
    stopped = []
    main.install_stop_handlers(Loop(), lambda: stopped.append(True))
    assert set(registered) == {signal.SIGINT, signal.SIGTERM}
    registered[signal.SIGINT](signal.SIGINT, None)
    assert stopped == [True]


def test_uninstallable_signal_is_skipped(monkeypatch):
    class Loop:
        def add_signal_handler(self, sig, callback):
            raise NotImplementedError

    def fake_signal(sig, handler):
        if sig == signal.SIGTERM:
            raise ValueError("unsupported")
        if sig == signal.SIGINT:
            raise OSError("unsupported")
        return signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)
    main.install_stop_handlers(Loop(), lambda: None)


def test_amain_gets_past_windows_signal_setup(monkeypatch):
    class Engine:
        def __init__(self, cfg, record_only=False):
            self.record_only = record_only

        def request_stop(self):
            return None

        async def run(self):
            return None

    monkeypatch.setattr(main, "Engine", Engine)
    registered = []

    async def go():
        loop = asyncio.get_running_loop()

        def raise_ni(sig, callback, *args):
            raise NotImplementedError

        monkeypatch.setattr(loop, "add_signal_handler", raise_ni)

        def fake_signal(sig, handler):
            registered.append(sig)
            return signal.SIG_DFL

        monkeypatch.setattr(signal, "signal", fake_signal)
        await main.amain(object(), True, False, False, None, "en")

    asyncio.run(go())
    # asyncio's own SIGINT restore may also call signal.signal on the way out.
    assert signal.SIGINT in registered
    assert signal.SIGTERM in registered


def test_startup_error_prints_type_and_traceback(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "main.py", "--record-only", "--symbol", "SNDK",
        "--hedge", "lighter", "--no-dashboard",
    ])

    class Cfg:
        dashboard = False
        log_level = "WARNING"

    monkeypatch.setattr(main, "load_config", lambda *a, **k: Cfg())

    def fake_run(coro):
        coro.close()
        raise NotImplementedError

    monkeypatch.setattr(main.asyncio, "run", fake_run)
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "startup error: NotImplementedError: NotImplementedError()" in err
    assert "Traceback (most recent call last):" in err
