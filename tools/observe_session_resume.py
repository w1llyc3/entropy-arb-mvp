"""Observe a natural Windows sleep/resume with the fake workbench. Never suspend.

No account configuration, exchange network or recorder control. A scheduler gap
alone cannot pass: correlate Windows power events with the observed gap.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from web.workbench import create_app
from soak_session import VirtualFeed,source_digest
from entropy_arb.release_gate import candidate_digest


def power_events(start,end):
    if os.name!='nt':return []
    # Only IDs/provider/timestamps, never full event messages or account names.
    a=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(start))
    b=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(end))
    script=("Get-WinEvent -FilterHashtable @{LogName='System';StartTime=[datetime]'"+a+
        "';EndTime=[datetime]'"+b+"';Id=42,107,1} -ErrorAction SilentlyContinue | "
        "Where-Object {$_.ProviderName -in @('Microsoft-Windows-Kernel-Power','Microsoft-Windows-Power-Troubleshooter')} | "
        "ForEach-Object {[pscustomobject]@{id=$_.Id;provider=$_.ProviderName;utc=$_.TimeCreated.ToUniversalTime().ToString('o')}} | ConvertTo-Json")
    try:
        result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],
            capture_output=True,text=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        rows=json.loads(result.stdout) if result.stdout.strip() else []
        return rows if isinstance(rows,list) else [rows]
    except Exception:return []


def observed_sleep(rows):
    sleep=[r for r in rows if r.get('provider')=='Microsoft-Windows-Kernel-Power' and r.get('id')==42]
    wake=[r for r in rows if (r.get('provider')=='Microsoft-Windows-Kernel-Power' and r.get('id')==107) or
          (r.get('provider')=='Microsoft-Windows-Power-Troubleshooter' and r.get('id')==1)]
    try:
        parse=lambda r:datetime.fromisoformat(r['utc'].replace('Z','+00:00'))
        return any((parse(w)-parse(s)).total_seconds()>=15 for s in sleep for w in wake)
    except (ValueError,KeyError,TypeError):return False


def observe(state,seconds,progress):
    feed=VirtualFeed();started=time.monotonic();prior=time.time();armed=False;gap_start=None
    with TestClient(create_app(state_dir=state,data_root=state/'no-market-data',paper_feed=feed)) as client:
        session=client.post('/api/sessions',json=dict(key='resume-observer-create',rules=dict(run_mode='PAPER',duration_minutes=1440,entry_bps=10000))).json()
        sid=session['id']
        assert client.post(f'/api/sessions/{sid}/start',json=dict(key='resume-observer-start',version=1)).status_code==200
        written=0
        while time.monotonic()-started<seconds:
            data=client.get('/api/status').json();now=time.time()
            if data['active'] is None:
                return dict(status='INCONCLUSIVE_SESSION_EXPIRED',passed=False,real_windows_sleep_verified=False,elapsed_sec=time.monotonic()-started)
            if now-prior>15:gap_start=prior
            armed=armed or (data['active']['state']=='OBSERVING' and bool(feed.books) and all(b.ready for b in feed.books.values()))
            if data['active']['state']=='RECOVERY_REQUIRED':
                rows=power_events((gap_start if gap_start is not None else prior)-5,now+5)
                clock_event=any(e['kind']=='CLOCK_INTERRUPTION_REQUIRES_REVIEW' for e in data['events'])
                invalidated=all(not book.ready for book in feed.books.values())
                checks=bool(armed and clock_event and not data['paper']['orders'] and invalidated)
                verified=os.name=='nt' and checks and observed_sleep(rows)
                return dict(status='PASSED' if verified else 'INCONCLUSIVE_GAP_WITHOUT_POWER_PROOF',
                            passed=verified,real_windows_sleep_verified=verified,
                            clock_recovery_observed=clock_event,books_invalidated=invalidated,
                            fake_orders=len(data['paper']['orders']),power_events=rows,
                            elapsed_sec=time.monotonic()-started)
            prior=now
            if time.monotonic()-written>=10:
                progress(dict(running=True,status='WAITING_FOR_NATURAL_SLEEP',passed=False,
                              real_windows_sleep_verified=False,armed=armed,elapsed_sec=time.monotonic()-started))
                written=time.monotonic()
            time.sleep(.2)
    return dict(status='INCONCLUSIVE_NO_SLEEP',passed=False,real_windows_sleep_verified=False,
                elapsed_sec=time.monotonic()-started)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=90000)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if not 0<args.seconds<=90000:parser.error('duration must be 0–90000 seconds')
    output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():parser.error('preserve existing evidence; use a fresh output')
    state=output.with_suffix('.state');state.mkdir(exist_ok=False)
    meta=dict(mode='FAKE_SESSION_WINDOWS_OBSERVER',pid=os.getpid(),source_sha256=source_digest(),candidate_sha256=candidate_digest(ROOT),
              real_orders_sent=0,accounts_loaded=False,collector_touched=False,suspend_requested=False,
              started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),state_dir=str(state))
    def progress(item):
        tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(dict(meta,**item),indent=2),encoding='utf-8');tmp.replace(output)
    result=observe(state,args.seconds,progress);result['running']=False;progress(result)
    print(json.dumps(result))
    return 0 if result['passed'] else 2


if __name__=='__main__':raise SystemExit(main())
