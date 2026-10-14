"""Public market data only. No venue trading adapter, account or signer imports."""
import asyncio
import csv
import json
import logging
import os
from pathlib import Path
import time

import aiohttp

from .book import OrderBook
from .feeds import HLBookFeed,LighterBookFeed
from .recorder import MinuteRecorder
from .watchlist import ASSETS,resolve_catalog

log=logging.getLogger("research")
HL_INFO="https://api.hyperliquid.xyz/info"
HL_WS="wss://api.hyperliquid.xyz/ws"
LIGHTER_BASE="https://mainnet.zklighter.elliot.ai"


class SingleVenueRecorder:
    """Persist one-sided prices without inventing a hedge or a basis."""
    HEADER=("minute_ts","entropy_bid","entropy_ask","entropy_usd","samples")

    def __init__(self,path,book,pause_file,staleness=10):
        self.path,self.book,self.pause_file=Path(path),book,Path(pause_file)
        self.staleness=staleness
        self.pending=None

    def flush(self):
        if self.pending is None: return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        new=not self.path.exists() or self.path.stat().st_size==0
        if not new:
            with self.path.open(encoding="utf-8",newline="") as source:
                if next(csv.reader(source),None)!=list(self.HEADER): raise ValueError("single-venue CSV schema mismatch")
        with self.path.open("a",encoding="utf-8",newline="") as out:
            writer=csv.writer(out)
            if new: writer.writerow(self.HEADER)
            writer.writerow(self.pending)
        self.pending=None

    def sample(self,now=None):
        now=time.time() if now is None else now
        minute=int(now//60)*60
        if self.pending is not None and self.pending[0]!=minute: self.flush()
        if self.pause_file.exists() or not self.book.is_fresh(self.staleness): return
        bid,ask=self.book.best_bid(),self.book.best_ask()
        if bid is None or ask is None or bid<=0 or ask<bid: return
        count=self.pending[-1]+1 if self.pending else 1
        self.pending=(minute,bid,ask,(bid+ask)/2,count)

    async def run(self,stop):
        try:
            while not stop.is_set():
                self.sample()
                try: await asyncio.wait_for(stop.wait(),1)
                except asyncio.TimeoutError: pass
        finally: self.flush()


class ResearchCollector:
    """Extra nine assets; SNDK stays with the existing service collector."""
    def __init__(self,root,pause_file):
        self.root,self.pause_file=Path(root),Path(pause_file)
        self.task=None; self.stop=None; self.rows=[]; self.books={}; self.workers=[]
        self.error=None; self.catalog_at=None

    def start(self):
        if self.task and not self.task.done(): return
        self.stop=asyncio.Event()
        self.task=asyncio.create_task(self.run(),name="research-record-only")

    async def catalog(self,session):
        async with session.post(HL_INFO,json=dict(type="meta",dex="io")) as r:
            r.raise_for_status(); hl=await r.json()
        async with session.get(LIGHTER_BASE+"/api/v1/orderBooks") as r:
            r.raise_for_status(); lighter=await r.json()
        return resolve_catalog(hl,lighter)

    def status(self,primary_books=None):
        books=dict(self.books)
        if primary_books:
            books["SNDK"]=dict(entropy=primary_books.get("entropy"),hedge=primary_books.get("lighter"))
        def fresh(symbol,leg):
            book=books.get(symbol,{}).get(leg)
            return bool(book and book.is_fresh(10))
        return dict(mode="RECORD_ONLY",running=bool(self.task and not self.task.done()),
                    catalog_at=self.catalog_at,error=self.error,assets=[dict(row,
                    entropy_fresh=fresh(row["symbol"],"entropy"),hedge_fresh=fresh(row["symbol"],"hedge")) for row in self.rows])

    def _save_catalog(self):
        path=self.root/"logs"/"record-only"/"catalog.json"
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(observed_at=self.catalog_at,assets=self.rows),indent=2),encoding="utf-8")
        os.replace(temporary,path)

    async def run(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            while not self.stop.is_set():
                try:
                    self.rows=await self.catalog(session); self.catalog_at=time.time(); self.error=None
                    self._save_catalog()
                    for row in self.rows:
                        symbol=row["symbol"]
                        if symbol=="SNDK" or row["state"]=="UNAVAILABLE": continue
                        entropy=OrderBook(); pair=dict(entropy=entropy); self.books[symbol]=pair
                        feed=HLBookFeed("research "+symbol,HL_WS,row["entropy_coin"],entropy,lambda:None)
                        coroutines=[feed.run(self.stop)]
                        if row["state"]=="PAIRED":
                            hedge=OrderBook(); pair["hedge"]=hedge
                            coroutines.append(LighterBookFeed("research "+symbol,LIGHTER_BASE.replace("https:","wss:")+"/stream",
                                row["market_id"],hedge,lambda:None).run(self.stop))
                            # Separate namespace keeps old single-sided history intact
                            # if a missing counterpart is listed in the future.
                            recorder=MinuteRecorder(str(self.root/row["csv_path"]),entropy,hedge,10,pause_file=self.pause_file)
                        else:
                            recorder=SingleVenueRecorder(self.root/row["csv_path"].replace("minutes.csv","entropy-only.csv"),entropy,self.pause_file)
                        coroutines.append(recorder.run(self.stop))
                        self.workers.extend(asyncio.create_task(c,name=f"research-{symbol}-{i}") for i,c in enumerate(coroutines))
                    # If an I/O worker fails, tear down all of this collector's
                    # streams before retrying; never leave an unobserved task.
                    stopped=asyncio.create_task(self.stop.wait())
                    try:
                        done,_=await asyncio.wait([stopped,*self.workers],return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            if task is not stopped: task.result(); raise RuntimeError("research worker stopped")
                    finally:
                        stopped.cancel(); await asyncio.gather(stopped,return_exceptions=True)
                    return
                except asyncio.CancelledError: raise
                except Exception as exc:
                    self.error=type(exc).__name__; log.warning("research recorder retry: %s",self.error)
                finally:
                    for task in self.workers: task.cancel()
                    await asyncio.gather(*self.workers,return_exceptions=True); self.workers=[]; self.books={}
                try: await asyncio.wait_for(self.stop.wait(),15)
                except asyncio.TimeoutError: pass

    async def close(self):
        if self.stop: self.stop.set()
        if self.task:
            self.task.cancel(); await asyncio.gather(self.task,return_exceptions=True); self.task=None
