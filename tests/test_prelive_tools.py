import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).parents[1]


def test_new_session_soak_preserves_evidence_without_claiming_24_hours(tmp_path):
    output=tmp_path/'soak.json'
    result=subprocess.run([sys.executable,'tools/soak_session.py','--seconds','2','--cycle-sec','1','--output',str(output)],
                          cwd=ROOT,capture_output=True,timeout=25)
    assert result.returncode==0,result.stderr
    report=json.loads(output.read_text())
    assert report['passed'] and not report['qualifies_as_24h_soak']
    assert not report['accounts_loaded'] and not report['collector_touched']
    assert (Path(report['state_dir'])/'sessions.sqlite').exists()
    assert report['source_sha256'] and report['ledger_sha256']


def test_windows_observer_without_sleep_stays_inconclusive(tmp_path):
    output=tmp_path/'resume.json'
    result=subprocess.run([sys.executable,'tools/observe_session_resume.py','--seconds','1','--output',str(output)],
                          cwd=ROOT,capture_output=True,timeout=20)
    assert result.returncode==2,result.stderr
    report=json.loads(output.read_text())
    assert not report['passed'] and not report['real_windows_sleep_verified']
    assert not report['suspend_requested'] and not report['collector_touched']


def test_power_proof_requires_a_real_ordered_long_enough_pair():
    sys.path.insert(0,str(ROOT/'tools'))
    try:
        from observe_session_resume import observed_sleep
        sleep=dict(provider='Microsoft-Windows-Kernel-Power',id=42,utc='2026-10-17T00:00:00Z')
        wake=dict(provider='Microsoft-Windows-Kernel-Power',id=107,utc='2026-10-17T00:00:20Z')
        assert observed_sleep([sleep,wake])
        assert not observed_sleep([wake])
        assert not observed_sleep([sleep,dict(wake,utc='2026-10-17T00:00:02Z')])
    finally:sys.path.pop(0)
