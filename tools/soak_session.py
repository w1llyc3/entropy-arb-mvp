"""Actual workbench lifespan/API soak with virtual depth only. No accounts/network.

Run with a fresh output path; retain the Session database and progress evidence.
This is separate from the older TradingService soak.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from entropy_arb.book import OrderBook
from web.workbench import create_app
from entropy_arb.release_gate import candidate_digest

SOURCES=('web/workbench.py','entropy_arb/workbench.py','entropy_arb/paper_session.py',
         'entropy_arb/session_reconciliation.py','entropy_arb/session_books.py',
         'entropy_arb/book.py','entropy_arb/feeds.py','entropy_arb/continuity.py')


def source_digest():
    return hashlib.sha256(b''.join(name.encode()+b'\0'+(ROOT/name).read_bytes().replace(b'\r\n',b'\n') for name in SOURCES)).hexdigest()


class VirtualFeed:
    def __init__(self):self.books={};self.closing=False
    async def select(self,symbol,http):
        self.books={}
        if not symbol:return
        for venue,bid in (('entropy',100.5 if self.closing else 100),('lighter',100.5 if self.closing else 101)):
            book=OrderBook();book.apply_hl([[dict(px=str(bid),sz='10')],[dict(px=str(bid+.01),sz='10')]])
            self.books[venue]=book
    async def close(self):self.books={}


def wait(client,predicate):
    until=time.monotonic()+10
    while time.monotonic()<until:
        result=client.get('/api/status');assert result.status_code==200
        result=result.json()
        if predicate(result):return result
        time.sleep(.02)
    raise AssertionError('workbench condition timeout')


def soak(state,seconds,cycle_sec,progress):
    cycles=restarts=lost_ack_checks=0;client=None;started=time.monotonic();failure=None;phase='connect'
    def connect():
        feed=VirtualFeed()
        app=create_app(state_dir=state,data_root=state/'absent-market-data',paper_feed=feed)
        c=TestClient(app);c.__enter__()
        return c,app,feed
    try:
        client,app,feed=connect()
        while time.monotonic()-started<seconds:
            tick=time.monotonic();cycle=cycles+1
            phase='create';feed.closing=False
            rule=dict(run_mode='PAPER',duration_minutes=1440,max_inventory_usd=11,fee_entropy_bps=0,fee_lighter_bps=0,slippage_bps=0,funding_buffer_bps=0)
            response=client.post('/api/sessions',json=dict(key=f'soak:create:{cycle}',rules=rule));assert response.status_code==200
            sid=response.json()['id']
            if cycle%7==0:app.state.paper.faults={'entropy':'lose_ack'}
            body=dict(key=f'soak:start:{cycle}',version=1)
            for _ in range(5):assert client.post(f'/api/sessions/{sid}/start',json=body).status_code==200
            phase='entry';snapshot=wait(client,lambda s:len(s.get('paper',{}).get('orders',[]))==2)
            if cycle%5==0:
                phase='restart'
                client.__exit__(None,None,None);client=None
                client,app,feed=connect();restarts+=1
                snapshot=client.get('/api/status').json()
                assert snapshot['active']['state']=='RECOVERY_REQUIRED'
            if snapshot['active']['state']=='RECOVERY_REQUIRED':
                phase='recovery'
                payload=dict(key=f'soak:query:{cycle}',version=snapshot['active']['version'])
                for _ in range(5):assert client.post(f'/api/sessions/{sid}/reconcile-paper',json=payload).status_code==200
                snapshot=client.get('/api/status').json()
                assert not snapshot['paper']['recovery_required']
                assert len(snapshot['paper']['orders'])==2
                payload=dict(key=f'soak:resume:{cycle}',version=snapshot['active']['version'])
                assert client.post(f'/api/sessions/{sid}/resume',json=payload).status_code==200
                if cycle%7==0:lost_ack_checks+=1
            phase='drain';app.state.paper.faults={};feed.closing=True
            snapshot=client.get('/api/status').json()
            payload=dict(key=f'soak:end:{cycle}',version=snapshot['active']['version'])
            assert client.post(f'/api/sessions/{sid}/end',json=payload).status_code==200
            wait(client,lambda s:s['active'] is None)
            report=client.get(f'/api/sessions/{sid}/report').json()
            assert report['status']=='PAPER_SIMULATION_REPORT'
            assert report['paper']['flat'] and report['paper']['reconciliation']['complete']
            assert len(report['paper']['orders'])==4 and len(report['paper']['fills'])==4
            assert report['paper']['actual_exchange_orders_sent']==0
            cycles+=1
            progress(dict(running=True,passed=False,cycles=cycles,restarts=restarts,
                          lost_ack_checks=lost_ack_checks,elapsed_sec=time.monotonic()-started,
                          qualifies_as_24h_soak=False))
            time.sleep(max(0,min(cycle_sec-(time.monotonic()-tick),seconds-(time.monotonic()-started))))
    except Exception as exc:
        failure=type(exc).__name__
    finally:
        if client:client.__exit__(None,None,None)
    elapsed=time.monotonic()-started
    return dict(running=False,passed=failure is None and cycles>0 and elapsed>=seconds,
                cycles=cycles,restarts=restarts,lost_ack_checks=lost_ack_checks,failure=failure,
                failure_phase=phase if failure else None,
                elapsed_sec=elapsed,qualifies_as_24h_soak=failure is None and seconds>=86400 and elapsed>=86400,
                real_orders_sent=0,real_windows_sleep_verified=False,real_exchange_settlement_verified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=86400)
    parser.add_argument('--cycle-sec',type=float,default=30)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    if args.seconds<=0 or args.cycle_sec<1:parser.error('positive seconds and cycle >=1 required')
    output=Path(args.output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():parser.error('preserve existing evidence; use a fresh output path')
    state=output.with_suffix('.state');state.mkdir(exist_ok=False)
    metadata=dict(mode='PAPER_WORKBENCH_SOAK',pid=os.getpid(),requested_sec=args.seconds,
                  started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),state_dir=str(state),
                  commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                  source_sha256=source_digest(),candidate_sha256=candidate_digest(ROOT),accounts_loaded=False,collector_touched=False)
    def progress(item):
        temporary=output.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(metadata,**item),indent=2),encoding='utf-8');temporary.replace(output)
    progress(dict(running=True,passed=False,cycles=0,qualifies_as_24h_soak=False))
    result=soak(state,args.seconds,args.cycle_sec,progress)
    result['completed_utc']=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())
    result['ledger_sha256']=hashlib.sha256((state/'sessions.sqlite').read_bytes()).hexdigest()
    progress(result)
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
