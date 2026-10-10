"""Watchdog must not spawn a second panel while 8765 is already up."""
import os
import socket
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.watchdog import (  # noqa: E402
    WatchdogLock, http_status_ok, panel_is_healthy, run_loop, tcp_listening,
    tick,
)

ROOT = os.path.join(os.path.dirname(__file__), "..")
BAT = os.path.join(ROOT, "tools", "run_web_watchdog.bat")


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_tick_does_not_spawn_when_healthy_or_child_lives():
    spawned = []

    def spawn():
        spawned.append(1)
        return "child"

    assert tick(True, None, spawn) is None
    assert spawned == []
    assert tick(False, None, spawn) == "child"
    assert spawned == [1]

    class Alive:
        def poll(self):
            return None

    assert tick(False, Alive(), spawn) is not None
    assert spawned == [1]


def test_down_port_is_not_healthy():
    port = _free_port()
    assert tcp_listening("127.0.0.1", port, timeout=0.2) is False
    assert http_status_ok("127.0.0.1", port, timeout=0.2) is False
    assert panel_is_healthy("127.0.0.1", port, timeout=0.2) is False


def test_listening_socket_counts_as_healthy():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert panel_is_healthy("127.0.0.1", port, timeout=0.5) is True
    finally:
        sock.close()


def test_http_status_counts_as_healthy():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"running": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert http_status_ok("127.0.0.1", port, timeout=1.0) is True
        assert panel_is_healthy("127.0.0.1", port, timeout=1.0) is True
    finally:
        server.shutdown()
        server.server_close()


def test_second_watchdog_does_not_take_the_lock(tmp_path):
    import time
    lock_path = tmp_path / ".web" / "watchdog.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {ROOT!r})
            from pathlib import Path
            from web.watchdog import WatchdogLock
            lock = WatchdogLock(Path({str(lock_path)!r}))
            assert lock.acquire()
            time.sleep(30)
        """)],
        cwd=ROOT,
    )
    try:
        start = time.time()
        while time.time() - start < 5:
            if lock_path.is_file() and lock_path.read_text().strip().isdigit():
                break
            if holder.poll() is not None:
                raise AssertionError("holder exited before locking")
            time.sleep(0.02)
        else:
            raise AssertionError("holder did not publish its pid")
        assert WatchdogLock(lock_path).acquire() is False
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_run_loop_exits_when_lock_is_held(tmp_path):
    lock = WatchdogLock(tmp_path / ".web" / "watchdog.lock")
    assert lock.acquire() is True
    try:
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys
                sys.path.insert(0, {ROOT!r})
                from web.watchdog import run_loop
                raise SystemExit(run_loop(
                    root={str(tmp_path)!r},
                    spawn=lambda: (_ for _ in ()).throw(AssertionError("spawned")),
                    healthy=lambda: False,
                ))
            """)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    finally:
        lock.release()
    assert proc.returncode == 0, proc.stderr
    assert "already running" in proc.stdout


def test_bat_delegates_to_the_single_instance_helper():
    text = open(BAT, encoding="utf-8").read()
    assert "python -m web.watchdog" in text
    assert "8765" in text
    assert "python -m web\n" not in text.replace("python -m web.watchdog", "")
    assert "goto" not in text.lower()
