"""Credential-free service soak. Default 24h; output is evidence, not live release."""
import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import subprocess
import hashlib
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from entropy_arb.book import OrderBook
from entropy_arb.service import TradingService
from fakes.venues import FakeVenue


def books(entropy=1601):
    result={}
    for name,bid in [('entropy',entropy),('lighter',1599)]:
        book=OrderBook()
        book.apply_hl([[dict(px=str(bid),sz='10')],[dict(px=str(bid+.1),sz='10')]])
        book.funding=0; book.funding_period_sec=3600; book.funding_observed_ts=time.time()
        result[name]=book
    return result


async def proposal(service):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        candidate=service.snapshot().get('proposal')
        if candidate: return candidate
        await asyncio.sleep(.02)
    raise AssertionError('proposal deadline exceeded')


async def soak(root,seconds,cycle_sec,progress=None):
    venues={n:FakeVenue(venue=n,account='soak-'+n) for n in ('entropy','lighter')}
    book_state=books()
    def service():
        return TradingService(root,venues=venues,books=book_state,simulation=True,
            interval=.02,reconcile_interval=1,persist_sec=.05,lock_dir=root/'locks',in_rth=lambda:True)
    svc=service(); await svc.start()
    started=time.monotonic(); cycles=0; restarts=0; failure=None
    try:
        await svc.call('task',dict(mode='record',midline_bps=0,upper_bps=2,lower_bps=2,rth_only=False))
        while time.monotonic()-started<seconds:
            tick=time.monotonic()
            for closing in (False,True):
                book_state.clear(); book_state.update(books(1599 if closing else 1601))
                await svc.call('start')
                p=await proposal(svc)
                payload=dict(confirm=True,proposal_id=p['proposal_id'],idempotency_key='soak:'+p['proposal_id'])
                before=sum(v.submit_calls for v in venues.values())
                results=await asyncio.gather(*(svc.call('confirm',payload) for _ in range(5)))
                assert len({r['operation_id'] for r in results})==1
                assert sum(v.submit_calls for v in venues.values())-before==2
                assert svc.recovered and results[0]['operation']['state']=='SETTLED'
                await svc.call('pause')
            assert all(v.state.position==0 for v in venues.values())
            cycles+=1
            if cycles%5==0:
                await svc.close(); svc=service(); await svc.start(); restarts+=1
                assert svc.recovered
                # Recording is intentionally restored on restart. Pause before
                # the next explicit start rather than treating it as stopped.
                await svc.call('pause')
            if progress:
                progress(dict(running=True,passed=False,cycles=cycles,restarts=restarts,
                    elapsed_sec=time.monotonic()-started,metrics=svc.ledger.metrics(),
                    qualifies_as_24h_soak=False))
            await asyncio.sleep(max(0,cycle_sec-(time.monotonic()-tick)))
    except Exception as exc:
        failure=type(exc).__name__
    finally:
        metrics=svc.ledger.metrics()
        residual={n:str(v.state.position) for n,v in venues.items()}
        elapsed=time.monotonic()-started
        await svc.close()
    return dict(simulation=True,elapsed_sec=elapsed,requested_sec=seconds,cycles=cycles,restarts=restarts,
        metrics=metrics,residual_positions=residual,failure=failure,
        passed=failure is None and cycles>0 and all(Decimal(p)==0 for p in residual.values()) and elapsed>=seconds,
        qualifies_as_24h_soak=elapsed>=86400 and failure is None,
        real_windows_sleep_verified=False,real_exchange_contract_verified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=86400)
    parser.add_argument('--cycle-sec',type=float,default=30)
    parser.add_argument('--output',required=True)
    parser.add_argument('--state-dir',default=None,help='fresh isolated fake-state directory; never reuse a production path')
    args=parser.parse_args()
    if args.seconds<=0 or args.cycle_sec<5: parser.error('positive duration; cycle >=5sec to respect order budget')
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    state=Path(args.state_dir) if args.state_dir else output.with_suffix('.state')
    state.mkdir(parents=True,exist_ok=False) # Preserve existing evidence; never resume with an empty fake server.
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    metadata=dict(simulation=True,requested_sec=args.seconds,pid=os.getpid(),commit=commit,
                  state_dir=str(state.resolve()),started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()))
    def write_progress(item):
        temporary=output.with_suffix(output.suffix+'.tmp')
        temporary.write_text(json.dumps(dict(metadata,**item),indent=2),encoding='utf-8')
        temporary.replace(output)
    write_progress(dict(running=True,cycles=0,passed=False,qualifies_as_24h_soak=False))
    report=asyncio.run(soak(state,args.seconds,args.cycle_sec,write_progress))
    ledger=state/'.web/ledger.sqlite'
    report.update(running=False,ledger_sha256=hashlib.sha256(ledger.read_bytes()).hexdigest())
    write_progress(report)
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__=='__main__': raise SystemExit(main())
