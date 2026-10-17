"""Public account position display for the research watchlist.

No signer, order methods, execution locks or settlement/readiness claims.
Quantities on separate contracts are deliberately not netted.
"""
import asyncio
from decimal import Decimal
import io
from pathlib import Path
import re
import time

from dotenv import dotenv_values

from .watchlist import ASSETS


def public_identity(path):
    selected = []
    seen = set()
    aliases = {"HL_ACCOUNT_ADDRESS": "ENTROPY_HL_ACCOUNT"}
    with Path(path).open(encoding="utf-8-sig") as source:
        for line in source:
            key, sep, value = line.partition("=")
            key = key.strip().removeprefix("export ")
            key = aliases.get(key, key)
            if key not in ("ENTROPY_HL_ACCOUNT", "LIGHTER_ACCOUNT_INDEX"):
                continue  # Private/token fields never enter dotenv parsing.
            if key in seen:
                raise ValueError("duplicate public account configuration")
            seen.add(key)
            if sep:
                selected.append(key + "=" + value)
    fields = dotenv_values(stream=io.StringIO("\n".join(selected)), interpolate=False)
    accounts = dict(entropy=fields.get("ENTROPY_HL_ACCOUNT") or None,
                    lighter=fields.get("LIGHTER_ACCOUNT_INDEX") or None)
    if accounts["entropy"] and not re.fullmatch(r"0x[0-9a-fA-F]{40}", accounts["entropy"]):
        raise ValueError("invalid public account configuration")
    if accounts["lighter"] and not re.fullmatch(r"[0-9]+", accounts["lighter"]):
        raise ValueError("invalid public account configuration")
    if accounts["lighter"]:
        accounts["lighter"] = str(int(accounts["lighter"]))
    return accounts


def decimal(value):
    if type(value) not in (str, int, float):
        raise ValueError("numeric account field required")
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("finite account field required")
    return value


def hl_positions(data, orders):
    if not isinstance(data.get("assetPositions"), list) or not isinstance(orders, list):
        raise ValueError("incomplete public position response")
    available = decimal(data["withdrawable"])
    if available < 0:
        raise ValueError("invalid withdrawable balance")
    positions = {}
    for item in data["assetPositions"]:
        p = item["position"]
        if p["coin"] in positions:
            raise ValueError("duplicate position identity")
        positions[p["coin"]] = decimal(p["szi"])
    counts = {}
    order_ids = set()
    for order in orders:
        identity = str(order["oid"])
        if identity in order_ids:
            raise ValueError("duplicate open order identity")
        order_ids.add(identity)
        counts[order["coin"]] = counts.get(order["coin"], 0) + 1
    return {a.symbol: dict(position_qty=str(positions.get(a.coin, Decimal(0))),
                           available_usd=str(available), open_order_count=counts.get(a.coin, 0),
                           open_orders_verified=True) for a in ASSETS}


def lighter_positions(books, data, account):
    for payload in (books, data):
        if payload.get("code", 200) != 200:
            raise ValueError("public endpoint rejected account query")
    rows = [a for a in data["accounts"] if str(a["account_index"]) == str(account)]
    if len(rows) != 1 or not isinstance(rows[0].get("positions"), list):
        raise ValueError("incomplete public account response")
    available = decimal(rows[0]["available_balance"])
    if available < 0:
        raise ValueError("invalid available balance")
    active_markets = [b for b in books["order_books"]
                      if b.get("status") == "active" and b.get("market_type") == "perp"]
    market_ids = [int(b["market_id"]) for b in active_markets]
    if len(market_ids) != len(set(market_ids)):
        raise ValueError("ambiguous market identity")
    positions = {}
    for p in rows[0]["positions"]:
        mid = int(p["market_id"])
        if mid in positions:
            raise ValueError("duplicate position identity")
        sign, qty = decimal(p["sign"]), decimal(p["position"])
        if qty < 0 or sign not in (Decimal(-1), Decimal(1)):
            raise ValueError("unverified position sign")
        positions[mid] = sign * qty
    result = {}
    for asset in ASSETS:
        markets = [b for b in active_markets if b.get("symbol") == asset.lighter_symbol]
        if len(markets) != 1:
            continue  # Unresolved market is unknown, never an inferred zero.
        result[asset.symbol] = dict(position_qty=str(positions.get(int(markets[0]["market_id"]), Decimal(0))),
                                   available_usd=str(available), open_order_count=None,
                                   open_orders_verified=False)
    return result


class InventoryView:
    def __init__(self, accounts=None, *, now=time.time):
        self.accounts = accounts or {}
        self.now = now
        self.rows = {}
        self.observed = {}
        self.errors = {}

    async def refresh(self, session):
        async def hl():
            async def info(kind):
                async with session.post("https://api.hyperliquid.xyz/info", json=dict(
                        type=kind, user=self.accounts["entropy"], dex="io")) as response:
                    response.raise_for_status()
                    return await response.json()
            data, orders = await asyncio.gather(info("clearinghouseState"), info("openOrders"))
            return hl_positions(data, orders)

        async def lighter():
            async def get(path, params=None):
                async with session.get("https://mainnet.zklighter.elliot.ai/api/v1/" + path, params=params) as response:
                    response.raise_for_status()
                    return await response.json()
            books, data = await asyncio.gather(get("orderBooks"), get("account", dict(by="index", value=self.accounts["lighter"])))
            return lighter_positions(books, data, self.accounts["lighter"])

        async def update(venue, fetch):
            if not self.accounts.get(venue):
                return
            started = self.now()
            try:
                result = await asyncio.wait_for(fetch(), 8)
                self.rows[venue] = result
                self.observed[venue] = started  # Timestamp cannot be refreshed by GET polling.
                self.errors.pop(venue, None)
            except Exception:
                self.rows.pop(venue, None)
                self.observed.pop(venue, None)
                self.errors[venue] = "PUBLIC_READ_UNAVAILABLE"  # No exception bodies/identifiers.
        await asyncio.gather(update("entropy", hl), update("lighter", lighter))

    def snapshot(self, symbol):
        legs = {}
        for venue in ("entropy", "lighter"):
            observed = self.observed.get(venue)
            age = self.now() - observed if observed is not None else None
            fresh = age is not None and 0 <= age <= 30
            row = self.rows.get(venue, {}).get(symbol)
            legs[venue] = dict(configured=bool(self.accounts.get(venue)),
                               status="FRESH" if fresh and row else "UNKNOWN",
                               observed_at=observed, age_sec=age,
                               position_qty=row["position_qty"] if fresh and row else None,
                               available_usd=row["available_usd"] if fresh and row else None,
                               open_order_count=row["open_order_count"] if fresh and row else None,
                               open_orders_verified=bool(fresh and row and row["open_orders_verified"]))
        return dict(symbol=symbol, source="PUBLIC_EXCHANGE_POSITION_READ", legs=legs,
                    net_exposure_usd=None, contracts_equivalence_verified=False,
                    execution_ready=False, settlement_verified=False,
                    notice="Account-wide positions, not Session-attributed inventory. No cross-contract netting.")
