"""Bounded public paired-book smoke check. No accounts, env, orders or recorder."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import aiohttp
from entropy_arb.session_books import SessionBooks
from entropy_arb.paper_session import PaperSession
from entropy_arb.watchlist import asset_for


async def check(symbol,seconds):
    feed=SessionBooks()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7),trust_env=True) as http:
        try:
            await feed.select(symbol,http)
            until=time.monotonic()+seconds
            while time.monotonic()<until:
                if PaperSession.valid_books(feed.books,time.time()):
                    break
                await asyncio.sleep(.2)
            fresh=PaperSession.valid_books(feed.books,time.time())
            return dict(symbol=symbol,paired_depth_fresh=fresh,feed_error=feed.error,
                        accounts_loaded=False,real_orders_sent=0,collector_touched=False)
        finally:
            await feed.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol',default='SNDK')
    parser.add_argument('--seconds',type=float,default=15)
    args=parser.parse_args()
    asset_for(args.symbol)
    if not 0<args.seconds<=30:parser.error('bounded 0–30 seconds required')
    result=asyncio.run(check(args.symbol,args.seconds))
    print(json.dumps(result))
    raise SystemExit(0 if result['paired_depth_fresh'] else 1)
