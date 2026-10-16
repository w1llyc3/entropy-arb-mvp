"""Run a fake-only armed service while the operator manually sleeps Windows."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

from soak_execution import books,TradingService,FakeVenue


async def probe(root,seconds):
    venues={n:FakeVenue(venue=n,account='sleep-probe-'+n) for n in ('entropy','lighter')}
    states=books()
    service=TradingService(root,venues=venues,books=states,simulation=True,interval=.05,
        persist_sec=.05,lock_dir=root/'locks',in_rth=lambda:True)
    await service.start()
    started=time.monotonic(); had_proposal=False; event=None; checks=False
    try:
        await service.call('task',dict(mode='record',midline_bps=0,upper_bps=2,lower_bps=2,rth_only=False))
        await service.call('start')
        while time.monotonic()-started<seconds:
            snapshot=service.snapshot()
            had_proposal=had_proposal or bool(snapshot.get('proposal'))
            event=snapshot.get('continuity_fault')
            if event:
                checks=bool(had_proposal and service.ledger.proposal() is None
                    and all(not book.ready for book in states.values())
                    and all(v.submit_calls==0 for v in venues.values()))
                break
            await asyncio.sleep(.2)
        return dict(simulation=True,gap_observed=event is not None,continuity_fault=event,
            had_proposal_before_gap=had_proposal,checks_passed=checks,
            submits=sum(v.submit_calls for v in venues.values()),
            actual_windows_sleep_requires_operator_confirmation=True,
            elapsed_sec=time.monotonic()-started)
    finally: await service.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=300)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print('Fake service only. Once armed, manually sleep Windows for >20 seconds, then resume.')
    with tempfile.TemporaryDirectory(prefix='entropy-resume-probe-') as directory:
        result=asyncio.run(probe(Path(directory),args.seconds))
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
    return 0 if result['checks_passed'] else 2 # No gap is INCONCLUSIVE, not a pass.


if __name__=='__main__': raise SystemExit(main())
