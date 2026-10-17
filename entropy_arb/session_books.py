"""Independent public feed subscription for one selected PAPER Session.

Reuses the existing gap-aware feeds, writes no recorder files and has no account
or order APIs. Historical minutes can never be injected as execution books.
"""
import asyncio
import time

from .book import OrderBook
from .feeds import LighterBookFeed
from .watchlist import resolve_catalog


class SessionBooks:
    def __init__(self):
        self.symbol=None
        self.books={}
        self.tasks=[]
        self.stop=None
        self.error=None
        self.retry_after=0

    async def hl_poll(self,http,coin,book):
        # Do not consume an additional HL websocket slot alongside ten-asset
        # collection (official per-IP cap: 10). At most one REST poll per second.
        while not self.stop.is_set():
            try:
                async with http.post("https://api.hyperliquid.xyz/info",json=dict(type="l2Book",coin=coin)) as response:
                    response.raise_for_status();data=await response.json()
                observed=float(data["time"])/1000
                if data["coin"]!=coin or not 0<=time.time()-observed<=5:
                    raise ValueError("stale or mismatched public depth")
                book.apply_hl(data["levels"])
                book.last_update_ts=observed
            except asyncio.CancelledError:
                raise
            except Exception:
                book.clear()
            await asyncio.sleep(1)

    async def select(self, symbol, http):
        if self.symbol==symbol and self.tasks and not any(t.done() for t in self.tasks):
            return
        if self.symbol==symbol and symbol is not None and time.monotonic()<self.retry_after:
            for book in self.books.values(): book.clear()
            return
        await self.close()
        self.symbol=symbol
        self.retry_after=time.monotonic()+15
        if symbol is None:
            return
        try:
            async with http.post("https://api.hyperliquid.xyz/info",json=dict(type="meta",dex="io")) as response:
                response.raise_for_status();hl=await response.json()
            async with http.get("https://mainnet.zklighter.elliot.ai/api/v1/orderBooks") as response:
                response.raise_for_status();lighter=await response.json()
            row=next(r for r in resolve_catalog(hl,lighter) if r["symbol"]==symbol)
            if row["state"]!="PAIRED":
                self.error="PAIRED_MARKET_UNAVAILABLE"
                return
            self.books={v:OrderBook() for v in ("entropy","lighter")}
            self.stop=asyncio.Event()
            self.tasks=[asyncio.create_task(self.hl_poll(http,row["entropy_coin"],self.books["entropy"])),
                        asyncio.create_task(LighterBookFeed("paper "+symbol,"wss://mainnet.zklighter.elliot.ai/stream",row["market_id"],self.books["lighter"],lambda:None).run(self.stop))]
            self.error=None
        except Exception:
            self.error="PUBLIC_FEED_UNAVAILABLE"
            self.books={}

    async def close(self):
        if self.stop:
            self.stop.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.tasks=[]
        self.books={}
        self.symbol=None
