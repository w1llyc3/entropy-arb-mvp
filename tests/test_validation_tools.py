import json
from pathlib import Path
import subprocess
import sys

import pytest

from entropy_arb.live_transport import read_operator_env,verify_sdk_pins


def test_read_auth_loads_only_lighter_not_hl_secret(tmp_path):
    env=tmp_path/'synthetic.env'
    env.write_text('ENTROPY_HL_PRIVATE_KEY=hl-sentinel\nLIGHTER_API_PRIVATE_KEY=lighter-sentinel\nLIGHTER_API_KEY_INDEX=2\n')
    selected=read_operator_env(env,lighter_auth=True)
    assert 'ENTROPY_HL_PRIVATE_KEY' not in selected
    assert selected['LIGHTER_API_PRIVATE_KEY']=='lighter-sentinel'


def test_unpinned_signing_installation_cannot_pass_preflight(monkeypatch):
    class Installed:
        def read_text(self,name): return '{}'
    monkeypatch.setattr('entropy_arb.live_transport.metadata.distribution',lambda name:Installed())
    with pytest.raises(RuntimeError): verify_sdk_pins()


def test_no_real_sleep_is_inconclusive_not_success(tmp_path):
    out=tmp_path/'probe.json'
    result=subprocess.run([sys.executable,'tools/windows_resume_probe.py','--seconds','1','--output',str(out)],
        cwd=Path(__file__).parents[1],capture_output=True,timeout=15)
    assert result.returncode==2,result.stderr
    report=json.loads(out.read_text())
    assert report['submits']==0 and not report['gap_observed'] and not report['checks_passed']
