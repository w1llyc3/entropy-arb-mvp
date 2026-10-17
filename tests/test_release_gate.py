from datetime import datetime,timezone
import hashlib
import json
import time
import pytest

from entropy_arb.release_gate import evaluate,require_manual_entry_evidence
from entropy_arb.storage import RecoveryRequired


def artifacts(tmp_path,monkeypatch):
    monkeypatch.setattr('entropy_arb.release_gate.candidate_digest',lambda root:'current-source')
    root=tmp_path;directory=root/'.web';directory.mkdir()
    state=directory/'soak.state';state.mkdir();db=state/'sessions.sqlite';db.write_bytes(b'retained synthetic test ledger')
    now=time.time()
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    soak=dict(candidate_sha256='current-source',running=False,passed=True,qualifies_as_24h_soak=True,
              elapsed_sec=90000,requested_sec=86400,cycles=100,restarts=1,lost_ack_checks=1,
              real_orders_sent=0,accounts_loaded=False,started_utc=iso(now-90010),completed_utc=iso(now-10),
              state_dir=str(state),ledger_sha256=hashlib.sha256(db.read_bytes()).hexdigest())
    resume=dict(candidate_sha256='current-source',running=False,passed=True,real_windows_sleep_verified=True,
                clock_recovery_observed=True,books_invalidated=True,fake_orders=0,suspend_requested=False,
                real_orders_sent=0,accounts_loaded=False,power_events=[
                    dict(provider='Microsoft-Windows-Kernel-Power',id=42,utc=iso(now-30)),
                    dict(provider='Microsoft-Windows-Kernel-Power',id=107,utc=iso(now-10))])
    readonly=dict(candidate_sha256='current-source',observed_at=now,
                  sdk=dict(passed=True,platform='Windows',native_signer_loaded=True),
                  preflight=dict(flat=True,fee_policy=dict(allowed=True)),history=dict(
                      sampled_cumulative_fills_verified=True,history_stable_across_reads=True,
                      inactive_cursor_exhausted_twice=True,exposed_active_order_coverage_verified=True,active_orders=0))
    paths={}
    for name,row in dict(soak=soak,resume=resume,readonly=readonly).items():
        p=directory/(name+'.json');p.write_text(json.dumps(row));paths[name]=str(p)
    return root,paths,now


def test_valid_synthetic_evidence_only_qualifies_manual_entry(tmp_path,monkeypatch):
    root,paths,now=artifacts(tmp_path,monkeypatch)
    result=evaluate(root,paths,now=now)
    assert result['manual_entry_evidence_ready']
    assert not result['automatic_live_enabled'] and not result['fees_verified'] and not result['settlement_verified']


@pytest.mark.parametrize('fault',['short-soak','running','old-source','stale-read','no-power-events','wrong-ledger'])
def test_incomplete_stale_or_unrelated_evidence_cannot_open_gate(tmp_path,monkeypatch,fault):
    root,paths,now=artifacts(tmp_path,monkeypatch)
    kind='resume' if fault=='no-power-events' else 'readonly' if fault=='stale-read' else 'soak'
    path=paths[kind];row=json.loads(open(path).read())
    if fault=='short-soak':row['elapsed_sec']=1
    if fault=='running':row['running']=True
    if fault=='old-source':row['candidate_sha256']='previous-code'
    if fault=='stale-read':row['observed_at']=now-61
    if fault=='no-power-events':row['power_events']=[]
    if fault=='wrong-ledger':row['ledger_sha256']='wrong'
    with open(path,'w') as out:json.dump(row,out)
    assert not evaluate(root,paths,now=now)['manual_entry_evidence_ready']


def test_ready_flag_alone_cannot_bypass_artifact_recheck(tmp_path,monkeypatch):
    root,paths,now=artifacts(tmp_path,monkeypatch)
    out=root/'.web/prelive-validation';out.mkdir()
    (out/'release.json').write_text(json.dumps(dict(manual_entry_evidence_ready=True,artifacts={})))
    with pytest.raises(RecoveryRequired):require_manual_entry_evidence(root,accounts=dict(entropy='synthetic',lighter='10'))


def test_different_accounts_cannot_reuse_readonly_validation(tmp_path,monkeypatch):
    root,paths,now=artifacts(tmp_path,monkeypatch)
    result=evaluate(root,paths,now=now,accounts=dict(entropy='different',lighter='11'))
    assert not result['manual_entry_evidence_ready'] and not result['checks']['same_account_domain']


def test_windows_checkout_line_endings_do_not_change_functional_candidate(tmp_path,monkeypatch):
    from entropy_arb.release_gate import candidate_digest
    monkeypatch.setattr('entropy_arb.release_gate.FILES',('sample.py',))
    file=tmp_path/'sample.py';file.write_bytes(b'pass\n')
    digest=candidate_digest(tmp_path)
    file.write_bytes(b'pass\r\n')
    assert candidate_digest(tmp_path)==digest
    file.write_bytes(b'raise RuntimeError\r\n')
    assert candidate_digest(tmp_path)!=digest
