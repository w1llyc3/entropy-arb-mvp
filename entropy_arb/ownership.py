"""OS-held ownership. Never unlink a lock file, including after a crash."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


class OwnerLock:
    def __init__(self, scope, directory=None):
        directory = Path(directory or Path(tempfile.gettempdir()) / "entropy-arb-owners")
        self.path = directory / (hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest() + ".lock")
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0"); handle.flush()
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def close(self):
        if self.handle is None: return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close(); self.handle = None


def account_locks(accounts,directory=None):
    """Lock each venue/account, not only a pair (pairs may overlap one leg)."""
    return [OwnerLock(dict(venue=name,account=account,market='SNDK',deployment='io/lighter-mainnet'),directory)
            for name,account in sorted(accounts.items()) if account!='unconfigured']
