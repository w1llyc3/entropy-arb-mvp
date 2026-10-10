"""Single-instance watchdog for the localhost panel.

``tools/run_web_watchdog.bat`` starts this module. A second copy exits
immediately because the lock file is already held. When ``127.0.0.1:8765``
already accepts a TCP connection or answers ``GET /api/status``, this
process does not spawn another ``python -m web`` (that spawn is WinError
10048). It still starts the panel when the port is actually down, and
restarts after that child exits and the port is still down.
"""
from __future__ import annotations

import argparse
import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_ROOT = Path(__file__).resolve().parents[1]


class WatchdogLock:
    """Exclusive lock held for the life of the process. Do not unlink it.

    Unlinking lets a second process create a new inode and take another
    lock. A dead holder releases the lock when the OS closes the fd.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                # msvcrt locks a byte that has to exist. A brand-new file
                # is empty, and locking byte 0 then fails for every copy.
                fh.seek(0, os.SEEK_END)
                if fh.tell() < 1:
                    fh.write("\0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        # Do not truncate: shrinking the file can drop the locked byte.
        fh.seek(0)
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()


def tcp_listening(host: str, port: int, timeout: float = 0.4) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def http_status_ok(host: str, port: int, timeout: float = 0.8) -> bool:
    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        resp.read()
        return 200 <= resp.status < 300
    except OSError:
        return False
    finally:
        if conn is not None:
            conn.close()


def panel_is_healthy(host: str = HOST, port: int = DEFAULT_PORT,
                     timeout: float = 0.8) -> bool:
    """True when spawning another panel would hit "address already in use".

    A listening socket is enough. ``GET /api/status`` returning 2xx is
    also enough, for a probe that can speak HTTP when the TCP check raced.
    """
    if tcp_listening(host, port, timeout=min(timeout, 0.4)):
        return True
    return http_status_ok(host, port, timeout=timeout)


def child_running(child) -> bool:
    return child is not None and child.poll() is None


def tick(healthy: bool, child, spawn: Callable[[], object]):
    """One decision. Never spawns while the port is healthy or a child lives."""
    if child_running(child):
        return child
    if healthy:
        return None
    return spawn()


def default_spawn(root: Path = _ROOT):
    return subprocess.Popen(
        [sys.executable, "-m", "web"],
        cwd=str(root),
    )


def run_loop(host: str = HOST, port: int = DEFAULT_PORT, interval: float = 5.0,
             root: Optional[Path] = None,
             spawn: Optional[Callable[[], object]] = None,
             sleep: Callable[[float], None] = time.sleep,
             healthy: Optional[Callable[[], bool]] = None) -> int:
    root = Path(root) if root is not None else _ROOT
    lock = WatchdogLock(root / ".web" / "watchdog.lock")
    if not lock.acquire():
        print("web watchdog already running; not starting another", flush=True)
        return 0
    if spawn is None:
        spawn = lambda: default_spawn(root)  # noqa: E731
    if healthy is None:
        healthy = lambda: panel_is_healthy(host, port)  # noqa: E731
    child = None
    try:
        while True:
            child = tick(bool(healthy()), child, spawn)
            sleep(interval)
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep one localhost panel on 127.0.0.1:8765")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument(
        "--check", action="store_true",
        help="print healthy or down, and do not spawn")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.check:
        up = panel_is_healthy(HOST, args.port)
        print("healthy" if up else "down")
        return 0 if up else 1
    return run_loop(port=args.port, interval=args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
