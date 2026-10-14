"""Public-account reads only. No .env, keys, signing SDK, or submit method.

Live release remains disabled: these REST responses do not establish a
settlement watermark, and Lighter order history requires a separately
verified authenticated read contract.
"""
from decimal import Decimal
import re
import time

from .account_state import AccountSnapshot, TradeFill
from .execution_contract import OrderReport, OrderState


def number(value):
    result=Decimal(str(value))
    if not result.is_finite(): raise ValueError("nonfinite account field")
    return result


class PublicAccountReader:
    is_simulation=False

    def __init__(self, venue, account, session):
        self.venue,self.account,self.session=venue,account,session
        self._orders={}
        if venue=="entropy" and account and not re.fullmatch(r"0x[0-9a-fA-F]{40}",account):
            raise ValueError("public master account address required")
        if venue=="lighter" and account and not str(account).isdigit():
            raise ValueError("public Lighter account index required")

    async def _info(self, payload):
        async with self.session.post("https://api.hyperliquid.xyz/info",json=payload) as response:
            response.raise_for_status(); return await response.json()

    async def _lighter(self, path, params=None):
        async with self.session.get("https://mainnet.zklighter.elliot.ai/api/v1/"+path,params=params) as response:
            response.raise_for_status(); return await response.json()

    async def fetch_snapshot(self):
        if not self.account:
            return AccountSnapshot(self.venue,"unconfigured",None,None,time.time(),False)
        if self.venue=="entropy":
            data=await self._info(dict(type="clearinghouseState",user=self.account,dex="io"))
            orders=await self._info(dict(type="openOrders",user=self.account,dex="io"))
            if not isinstance(data.get("assetPositions"),list) or not isinstance(orders,list):
                raise ValueError("incomplete HL snapshot")
            position=Decimal(0)
            for item in data["assetPositions"]:
                p=item["position"]
                if p["coin"]=="io:SNDK": position=number(p["szi"])
            available=number(data["withdrawable"])
            number(data["marginSummary"]["accountValue"])
            open_orders=tuple(str(o.get("cloid") or o["oid"]) for o in orders if o["coin"]=="io:SNDK")
            return AccountSnapshot(self.venue,self.account,position,available,time.time(),True,open_orders)
        books=await self._lighter("orderBooks")
        matches=[b for b in books["order_books"] if b.get("symbol")=="SNDK" and b.get("status")=="active"]
        if len(matches)!=1: raise ValueError("unresolved SNDK market ID")
        market_id=int(matches[0]["market_id"])
        data=await self._lighter("account",dict(by="index",value=self.account))
        rows=[a for a in data["accounts"] if str(a.get("account_index"))==str(self.account)]
        if len(rows)!=1 or not isinstance(rows[0].get("positions"),list):
            raise ValueError("incomplete Lighter account")
        account=rows[0]; position=Decimal(0)
        for p in account["positions"]:
            if int(p["market_id"])==market_id:
                sign=number(p["sign"])
                if sign not in (Decimal(-1),Decimal(1)): raise ValueError("unknown position sign")
                position=sign*number(p["position"])
        # Public positions are useful for display, but not an order-history or
        # outstanding-order completeness guarantee. Fail closed for readiness.
        return AccountSnapshot(self.venue,self.account,position,number(account["available_balance"]),
                               time.time(),False)

    async def query(self, client_id):
        if not self.account or self.venue!="entropy" or not re.fullmatch(r"[0-9a-f]{32}",client_id):
            return OrderReport(client_id,OrderState.UNKNOWN,reason="read contract unavailable")
        data=await self._info(dict(type="orderStatus",user=self.account,oid="0x"+client_id))
        if data.get("status")!="order": return OrderReport(client_id,OrderState.UNKNOWN)
        order=data["order"]; inner=order["order"]
        if inner["coin"]!="io:SNDK" or str(inner.get("cloid","")).lower()!="0x"+client_id:
            return OrderReport(client_id,OrderState.UNKNOWN,reason="order identity unverified")
        self._orders[client_id]=(int(inner["oid"]),int(inner["timestamp"]),order["status"])
        fills=await self.query_fills(client_id)
        filled=sum((f.qty for f in fills),Decimal(0))
        status={"filled":OrderState.FILLED,"canceled":OrderState.CANCELED,
                "open":OrderState.PARTIAL if filled else OrderState.OPEN}.get(order["status"],OrderState.UNKNOWN)
        return OrderReport(client_id,status,filled,status!=OrderState.UNKNOWN)

    async def query_fills(self, client_id):
        if client_id not in self._orders: raise ValueError("order identity unresolved")
        oid,start,_=self._orders[client_id]
        rows=await self._info(dict(type="userFillsByTime",user=self.account,startTime=start,aggregateByTime=False))
        if not isinstance(rows,list) or len(rows)>=2000: raise ValueError("fill coverage unproven")
        fills=[]
        for r in rows:
            if r["coin"]=="io:SNDK" and int(r["oid"])==oid:
                if str(r["feeToken"]).strip()!="USDC": raise ValueError("fee currency unverified")
                fills.append(TradeFill(client_id,str(r["tid"]),number(r["sz"]),number(r["px"]),number(r["fee"])))
        return fills
