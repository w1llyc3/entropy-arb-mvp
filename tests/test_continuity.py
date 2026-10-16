import asyncio
from datetime import datetime,timezone

import pytest

from entropy_arb.continuity import ClockContinuity
from test_service import setup,armed,wait_for


@pytest.mark.parametrize('wall,mono,reason',[(101,51,None),(131,81,'scheduler gap or resume'),
    (131,51,'clock discontinuity'),(90,51,'clock discontinuity'),(101,49,'clock discontinuity')])
def test_elapsed_and_clock_jump_contract(wall,mono,reason):
    values=[100,50]
    clock=ClockContinuity(wall=lambda:values[0],monotonic=lambda:values[1])
    assert clock.observe() is None
    values[:]=wall,mono
    assert clock.observe()==reason


def test_resume_invalidates_books_proposals_and_does_not_submit(tmp_path):
    async def run():
        svc,venues,books=setup(tmp_path)
        await svc.start()
        try:
            await armed(svc)
            await wait_for(lambda:svc.snapshot().get('proposal'))
            svc.continuity.previous=(0,0)
            await svc.call('pause')
            assert all(not book.ready for book in books.values())
            assert svc.ledger.proposal() is None
            assert svc.snapshot()['continuity_fault']
            assert all(v.submit_calls==0 for v in venues.values())
        finally: await svc.close()
    asyncio.run(run())


@pytest.mark.parametrize('stamp,expected',[
    ('2026-11-26T16:00:00+00:00',False), # Thanksgiving
    ('2026-11-27T17:59:00+00:00',True),
    ('2026-11-27T18:00:00+00:00',False), # early close 13 ET
    ('2026-10-16T13:30:00+00:00',True),
    ('2026-10-16T20:00:00+00:00',False),
    ('2029-10-16T15:00:00+00:00',False), # calendar not verified
])
def test_service_regular_session_gate_uses_verified_calendar(tmp_path,monkeypatch,stamp,expected):
    svc,_,_=setup(tmp_path)
    svc.in_rth=None
    epoch=datetime.fromisoformat(stamp).timestamp()
    monkeypatch.setattr('entropy_arb.service.time.time',lambda:epoch)
    assert svc._in_rth() is expected
