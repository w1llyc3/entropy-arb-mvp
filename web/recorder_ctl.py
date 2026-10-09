"""Start and stop ``python3 main.py --record-only`` under a pid file.

The panel never builds a live-trading command. Symbol and hedge are the only
caller-supplied fields, and they are already validated before they get here.
A pid file plus a small JSON sidecar live in ``.web/`` so a restarted panel
can see a recorder it did not spawn itself.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_SECRET = re.compile(
    r"(0x[0-9a-fA-F]{16,}|PRIVATE|api_private|secret|BEGIN )",
    re.IGNORECASE,
)


def record_only_argv(symbol: str, hedge: str,
                     python: Optional[str] = None) -> list:
    """Argv for credential-free data collection. Always includes --record-only."""
    return [
        python or sys.executable,
        "main.py",
        "--record-only",
        "--no-dashboard",
        "--symbol", symbol,
        "--hedge", hedge,
    ]


class RecorderError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class _WindowsLock:
    """Release a ``msvcrt`` byte lock before the file is closed.

    Closing a still-locked fd on Windows raises ``PermissionError``.
    ``with`` on a plain file object only closes, so the Windows path
    cannot return the file the way ``fcntl.flock`` can.
    """

    def __init__(self, fh) -> None:
        self._fh = fh

    def __enter__(self):
        return self._fh

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()
        return False


class RecorderControl:
    def __init__(self, root: Path,
                 command_builder: Optional[Callable[[str, str], list]] = None
                 ) -> None:
        self.root = Path(root)
        self.command_builder = command_builder or record_only_argv
        self.dir = self.root / ".web"
        self.pid_path = self.dir / "recorder.pid"
        self.meta_path = self.dir / "recorder.json"
        self.log_path = self.dir / "recorder.log"
        self._lock_path = self.dir / "panel.lock"

    # ----------------------------------------------------------------- files

    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def _locked(self):
        self._ensure_dir()
        fh = open(self._lock_path, "a+")
        try:
            if sys.platform == "win32":
                # msvcrt locks a byte range from the current position.
                # The file is only a token, so every caller contends on byte 0.
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            fh.close()
            raise
        if sys.platform == "win32":
            return _WindowsLock(fh)
        return fh

    def _read_meta(self) -> Optional[dict]:
        if not self.meta_path.exists():
            return None
        try:
            data = json.loads(self.meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or "pid" not in data:
            return None
        return data

    def _write_meta(self, meta: dict) -> None:
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2) + "\n")
        os.replace(tmp, self.meta_path)
        self.pid_path.write_text(f"{int(meta['pid'])}\n")

    def _clear_meta(self) -> None:
        for path in (self.pid_path, self.meta_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    # --------------------------------------------------------------- process

    @staticmethod
    def _reap(pid: int) -> None:
        # Windows has no WNOHANG / waitpid reap. Liveness is os.kill(pid, 0).
        if sys.platform == "win32":
            return
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _cmdline(pid: int) -> Optional[list]:
        path = Path(f"/proc/{pid}/cmdline")
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        parts = [p.decode(errors="replace") for p in raw.split(b"\x00") if p]
        return parts or None

    def _matches(self, pid: int, argv: list) -> bool:
        """False when the pid was recycled into an unrelated process."""
        got = self._cmdline(pid)
        if got is None:
            # /proc unavailable: fall back to the signal check.
            return self._pid_exists(pid)
        return got == list(argv)

    def _alive(self, meta: Optional[dict]) -> bool:
        if not meta:
            return False
        try:
            pid = int(meta["pid"])
        except (TypeError, ValueError):
            return False
        self._reap(pid)
        if not self._pid_exists(pid):
            return False
        argv = meta.get("argv") or []
        if argv and not self._matches(pid, argv):
            return False
        return True

    def _signal(self, pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return

    def _terminate(self, pid: int) -> None:
        if not self._pid_exists(pid):
            self._reap(pid)
            return
        self._signal(pid, signal.SIGTERM)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            self._reap(pid)
            if not self._pid_exists(pid):
                return
            time.sleep(0.05)
        self._signal(pid, signal.SIGKILL)
        deadline = time.time() + 2.0
        while time.time() < deadline:
            self._reap(pid)
            if not self._pid_exists(pid):
                return
            time.sleep(0.05)

    def log_tail(self, limit: int = 15) -> list:
        if not self.log_path.exists():
            return []
        try:
            lines = self.log_path.read_text(errors="replace").splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-limit:]:
            if _SECRET.search(line):
                out.append("[redacted]")
            else:
                out.append(line[:500])
        return out

    # ------------------------------------------------------------------- api

    def snapshot(self) -> dict:
        """Process state only. CSV coverage is added by the panel."""
        with self._locked():
            meta = self._read_meta()
            running = self._alive(meta)
        now = time.time()
        pid = None
        started_at = None
        symbol = None
        hedge = None
        argv = None
        uptime = None
        if meta:
            try:
                pid = int(meta["pid"])
            except (TypeError, ValueError):
                pid = None
            started_at = meta.get("started_at")
            symbol = meta.get("symbol")
            hedge = meta.get("hedge")
            argv = meta.get("argv")
            if running and isinstance(started_at, (int, float)):
                uptime = max(0.0, now - float(started_at))
        warnings = []
        if meta and not running:
            label = f"pid {pid}" if pid else "the recorded pid"
            warnings.append(
                f"recorder process is dead ({label} is not running)")
        paused = bool(running and meta and meta.get("paused"))
        return {
            "running": running,
            "paused": paused,
            "pid": pid if meta else None,
            "uptime_sec": uptime,
            "started_at": started_at if meta else None,
            "symbol": symbol,
            "hedge": hedge,
            "argv": argv,
            "warnings": warnings,
            "log_tail": [] if running else self.log_tail(),
        }

    def start(self, symbol: str, hedge: str) -> dict:
        argv = list(self.command_builder(symbol, hedge))
        if self.command_builder is record_only_argv:
            if argv != record_only_argv(symbol, hedge) or "--record-only" not in argv:
                raise RecorderError("refusing to start without --record-only",
                                    status_code=500)
        if not argv or argv[0].startswith("-"):
            raise RecorderError("refusing to start an empty command",
                                status_code=500)
        main_py = self.root / "main.py"
        if self.command_builder is record_only_argv and not main_py.is_file():
            raise RecorderError(f"main.py not found in {self.root}")

        with self._locked():
            meta = self._read_meta()
            if self._alive(meta):
                raise RecorderError(
                    f"recorder already running (pid {meta['pid']})",
                    status_code=409)
            self._clear_meta()
            self._ensure_dir()
            with open(self.log_path, "ab") as logf:
                logf.write(
                    f"\n# panel start {' '.join(argv)}\n".encode())
                logf.flush()
                proc = subprocess.Popen(
                    argv,
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            meta = {
                "pid": proc.pid,
                "started_at": time.time(),
                "symbol": symbol,
                "hedge": hedge,
                "argv": argv,
            }
            try:
                self._write_meta(meta)
            except Exception:
                self._terminate(proc.pid)
                self._clear_meta()
                raise
        return {
            "running": True,
            "pid": proc.pid,
            "symbol": symbol,
            "hedge": hedge,
            "argv": argv,
        }

    def stop(self) -> dict:
        with self._locked():
            meta = self._read_meta()
            pid = None
            if meta:
                try:
                    pid = int(meta["pid"])
                except (TypeError, ValueError):
                    pid = None
            if pid:
                self._terminate(pid)
            self._clear_meta()
        return {"running": False, "stopped": True, "paused": False, "pid": pid}

    def pause(self) -> dict:
        """Freeze the record-only process. Does not send an order."""
        with self._locked():
            meta = self._read_meta()
            if not self._alive(meta):
                raise RecorderError("nothing is running to pause", status_code=409)
            pid = int(meta["pid"])
            self._signal(pid, signal.SIGSTOP)
            meta["paused"] = True
            self._write_meta(meta)
        return {"running": True, "paused": True, "pid": pid}

    def resume(self) -> dict:
        """Continue a paused record-only process."""
        with self._locked():
            meta = self._read_meta()
            if not self._alive(meta):
                raise RecorderError("nothing is paused to resume", status_code=409)
            if not meta.get("paused"):
                raise RecorderError("recorder is not paused", status_code=409)
            pid = int(meta["pid"])
            self._signal(pid, signal.SIGCONT)
            meta["paused"] = False
            self._write_meta(meta)
        return {"running": True, "paused": False, "pid": pid}
