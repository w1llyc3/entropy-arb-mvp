"""Disposable processes only; Windows byte locks must survive PID ambiguity."""
import os
from pathlib import Path
import subprocess
import sys

from entropy_arb.ownership import OwnerLock


def test_process_exit_releases_os_lock_without_deleting_file(tmp_path):
    scope=dict(account="fake-only",market="SNDK")
    lock=OwnerLock(scope,tmp_path)
    code="""
import os,sys
from entropy_arb.ownership import OwnerLock
lock=OwnerLock(dict(account='fake-only',market='SNDK'),sys.argv[1])
assert lock.acquire()
print('locked',flush=True)
sys.stdin.readline()
os._exit(7)
"""
    child=subprocess.Popen([sys.executable,"-c",code,str(tmp_path)],stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                           cwd=Path(__file__).resolve().parents[1])
    try:
        assert child.stdout.readline().strip()=="locked"
        assert not lock.acquire()
        child.communicate("exit\n",timeout=10)
        assert child.returncode==7
        assert lock.path.exists()
        assert lock.acquire()
    finally:
        lock.close()
        if child.poll() is None:
            child.kill(); child.communicate(timeout=10)
