import asyncio
from decimal import Decimal as D
import time

from entropy_arb.read_only import PublicAccountReader
from entropy_arb.execution_contract import OrderState


def test_hl_order_uses_exact_order_and_fill_identity_not_size_subtraction():
    async def run():
        reader=PublicAccountReader("entropy","0x"+"1"*40,None)
        async def info(payload):
            if payload["type"]=="orderStatus":
                return dict(status="order",order=dict(status="filled",order=dict(
                    coin="io:SNDK",cloid="0x"+"a"*32,oid=123,timestamp=1,origSz="999",sz="0")))
            return [dict(coin="io:SNDK",oid=123,sz=".007",px="1600",fee=".001008",tid=5,feeToken="USDC"),
                    dict(coin="xyz:SNDK",oid=123,sz="99",px="1600",fee="1",tid=6,feeToken="USDC")]
        reader._info=info
        report=await reader.query("a"*32)
        assert report.state==OrderState.FILLED and report.filled==D(".007")
        assert not hasattr(reader,"submit")
        fills=await reader.query_fills("a"*32)
        assert len(fills)==1 and fills[0].trade_id=="5"
    asyncio.run(run())


def test_lighter_symbol_resolves_market_id_and_remains_incomplete():
    async def run():
        reader=PublicAccountReader("lighter","10",None)
        async def get(path,params=None):
            if path=="orderBooks": return dict(order_books=[dict(symbol="SNDK",market_id=55,status="active")])
            return dict(accounts=[dict(account_index=10,available_balance="50",positions=[
                dict(market_id=55,position=".007",sign=-1)])])
        reader._lighter=get
        snap=await reader.fetch_snapshot()
        assert snap.position==D("-.007") and snap.available==50
        assert not snap.complete and snap.settled_through is None
        assert (await reader.query("a"*32)).state==OrderState.UNKNOWN
    asyncio.run(run())


def test_heartbeat_does_not_refresh_a_stale_book():
    from entropy_arb.book import OrderBook
    b=OrderBook()
    b.apply_hl([[dict(px="100",sz="1")],[dict(px="101",sz="1")]])
    b.last_update_ts=time.time()-60
    b.touch()
    assert not b.is_fresh(10)
