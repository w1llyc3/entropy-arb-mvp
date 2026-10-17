"""Evidence gate for the *first manual SNDK entry*, never an automatic live gate.

Read-only, no env/keys/network. Re-evaluate source, artifacts and freshness;
never trust a standalone ready=true checkbox. Reductions/cancels retain their
independent identity/position/ack checks and are not trapped behind soak age.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

FILES=('entropy_arb/release_gate.py','entropy_arb/live_adapter.py','entropy_arb/live_check.py',
       'entropy_arb/live_execution.py','entropy_arb/live_transport.py','entropy_arb/storage.py',
       'entropy_arb/execution.py','entropy_arb/history_audit.py','entropy_arb/ownership.py',
       'entropy_arb/workbench.py','entropy_arb/paper_session.py','entropy_arb/session_reconciliation.py',
       'entropy_arb/session_books.py','entropy_arb/book.py','entropy_arb/feeds.py',
       'entropy_arb/continuity.py','web/workbench.py','requirements-live.txt',
       'entropy_arb/account_state.py','entropy_arb/execution_contract.py','entropy_arb/inventory_view.py',
       'requirements.txt','requirements-web.txt','tools/check_live_sdk.py','tools/build_prelive_report.py',
       'tools/soak_session.py','tools/observe_session_resume.py','tools/prelive_readonly.py')


def candidate_digest(root):
    root=Path(root)
    return hashlib.sha256(b''.join(p.encode()+b'\0'+(root/p).read_bytes().replace(b'\r\n',b'\n') for p in FILES)).hexdigest()


def account_digest(accounts):
    return hashlib.sha256(json.dumps(accounts,sort_keys=True).encode()).hexdigest()


def read_artifact(root,path,suffix='.json'):
    path=Path(path).resolve();path.relative_to((Path(root)/'.web').resolve())
    if path.suffix!=suffix:raise ValueError('unexpected evidence artifact type')
    return path


def power_pair(events):
    parse=lambda r:datetime.fromisoformat(r['utc'].replace('Z','+00:00')).timestamp()
    sleeps=[r for r in events if r.get('provider')=='Microsoft-Windows-Kernel-Power' and r.get('id')==42]
    wakes=[r for r in events if (r.get('provider')=='Microsoft-Windows-Kernel-Power' and r.get('id')==107) or
           (r.get('provider')=='Microsoft-Windows-Power-Troubleshooter' and r.get('id')==1)]
    return any(parse(w)-parse(s)>=15 for s in sleeps for w in wakes)


def evaluate(root,paths,*,now=None,accounts=None):
    now=time.time() if now is None else now
    digest=candidate_digest(root);checks={};data={}
    for kind in ('soak','resume','readonly'):
        try:
            row=json.loads(read_artifact(root,paths[kind]).read_text(encoding='utf-8-sig'))
            same=row.get('candidate_sha256')==digest
            checks[kind+'_current_source']=same
            data[kind]=row
        except Exception:
            checks[kind+'_current_source']=False
            data[kind]={}
    soak=data['soak'];resume=data['resume'];reads=data['readonly']
    checks['session_soak_24h']=bool(soak.get('passed') is True and soak.get('running') is False
        and soak.get('qualifies_as_24h_soak') is True and soak.get('elapsed_sec',0)>=86400
        and soak.get('requested_sec',0)>=86400 and soak.get('cycles',0)>=100
        and soak.get('restarts',0)>0 and soak.get('lost_ack_checks',0)>0
        and soak.get('real_orders_sent')==0 and soak.get('accounts_loaded') is False)
    try:
        began=datetime.fromisoformat(soak['started_utc'].replace('Z','+00:00')).timestamp()
        ended=datetime.fromisoformat(soak['completed_utc'].replace('Z','+00:00')).timestamp()
        checks['soak_wall_clock_24h']=86400<=ended-began and began<=ended<=now
        db=read_artifact(root,Path(soak['state_dir'])/'sessions.sqlite','.sqlite')
        checks['soak_retained_ledger']=hashlib.sha256(db.read_bytes()).hexdigest()==soak['ledger_sha256']
    except Exception:
        checks['soak_wall_clock_24h']=False;checks['soak_retained_ledger']=False
    try:
        checks['real_windows_resume']=bool(resume.get('passed') is True and resume.get('running') is False
            and resume.get('real_windows_sleep_verified') is True and resume.get('clock_recovery_observed') is True
            and resume.get('books_invalidated') is True and resume.get('fake_orders')==0
            and resume.get('suspend_requested') is False and resume.get('real_orders_sent')==0
            and resume.get('accounts_loaded') is False and power_pair(resume.get('power_events',[])))
    except Exception:checks['real_windows_resume']=False
    checks['fresh_readonly_check']=0<=now-reads.get('observed_at',0)<=60
    if accounts is not None:checks['same_account_domain']=reads.get('account_domain')==account_digest(accounts)
    preflight=reads.get('preflight',{});history=reads.get('history',{});sdk=reads.get('sdk',{})
    checks['native_sdk']=sdk.get('passed') is True and sdk.get('platform')=='Windows' and sdk.get('native_signer_loaded') is True
    checks['flat_accounts_and_zero_fee_policy']=preflight.get('flat') is True and preflight.get('fee_policy',{}).get('allowed') is True
    checks['historical_execution_contract']=bool(history.get('sampled_cumulative_fills_verified') is True
        and history.get('history_stable_across_reads') is True and history.get('inactive_cursor_exhausted_twice') is True
        and history.get('exposed_active_order_coverage_verified') is True and history.get('active_orders')==0)
    blockers=[name for name,passed in checks.items() if not passed]
    return dict(manual_entry_evidence_ready=not blockers,automatic_live_enabled=False,
                checks=checks,blockers=blockers,candidate_sha256=digest,
                fees_verified=history.get('sampled_fee_fields_complete') is True,
                settlement_verified=False,scope='one reviewed manual SNDK entry only; financial/settlement evidence may remain pending')


def require_manual_entry_evidence(root,*,accounts):
    from .storage import RecoveryRequired
    try:
        path=Path(root)/'.web/prelive-validation/release.json'
        envelope=json.loads(path.read_text(encoding='utf-8'))
        result=evaluate(root,envelope['artifacts'],accounts=accounts)
    except Exception:
        raise RecoveryRequired('manual entry validation evidence missing or invalid') from None
    if not result['manual_entry_evidence_ready']:
        raise RecoveryRequired('manual entry validation gates closed: '+','.join(result['blockers']))
    return result
