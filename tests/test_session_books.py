import asyncio
import time
import pytest

from entropy_arb.book import OrderBook
from entropy_arb.session_books import SessionBooks


class Response:
    def __init__(self,payload):self.payload=payload
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def raise_for_status(self):pass
    async def json(self):return self.payload


@pytest.mark.parametrize('fault',[None,'stale','future','wrong-coin'])
def test_hl_rest_snapshot_requires_source_identity_and_timestamp(tmp_path,monkeypatch,fault):
    async def run():
        feed=SessionBooks();feed.stop=asyncio.Event();book=OrderBook()
        payload=dict(coin='io:SNDK',time=int(time.time()*1000),levels=[
            [dict(px='100',sz='1')],[dict(px='101',sz='1')]])
        if fault=='stale':payload['time']-=60000
        if fault=='future':payload['time']+=60000
        if fault=='wrong-coin':payload['coin']='io:NBIS'
        class HTTP:
            def post(self,url,json):
                assert json==dict(type='l2Book',coin='io:SNDK')
                return Response(payload)
        async def stop_after_one_poll(seconds):
            assert seconds==1
            feed.stop.set()
        monkeypatch.setattr('entropy_arb.session_books.asyncio.sleep',stop_after_one_poll)
        await feed.hl_poll(HTTP(),'io:SNDK',book)
        assert book.ready==(fault is None)
        if fault is None:assert book.last_update_ts==payload['time']/1000
    asyncio.run(run())


def test_missing_pair_backoff_makes_no_stream_and_does_not_retry_storm():
    async def run():
        feed=SessionBooks()
        class HTTP:
            count=0
            def post(self,url,json):
                self.count+=1
                return Response(dict(universe=[dict(name='io:IONQ')]))
            def get(self,url):return Response(dict(order_books=[]))
        http=HTTP()
        await feed.select('IONQ',http)
        await feed.select('IONQ',http)
        assert feed.error=='PAIRED_MARKET_UNAVAILABLE' and not feed.tasks
        assert http.count==1 and not feed.books
        await feed.close()
    asyncio.run(run())
