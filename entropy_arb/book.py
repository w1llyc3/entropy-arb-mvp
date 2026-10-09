"""Order book state and fee-aware arbitrage sizing.

One book class serves both feed protocols: zkLighter sends a snapshot plus
diffs (dict maintenance), Hyperliquid's l2Book sends full snapshots.
Freshness is connection-based (any inbound ws frame touches alive_ts): a quiet
market is not stale, only a dead feed is.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Sequence, Tuple

Level = Tuple[float, float]


class OrderBook:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ready = False
        self.last_update_ts = 0.0
        self.alive_ts = 0.0
        # Last funding rate fetched from a documented REST poll, or None.
        # clear() does not wipe it: a book resync is not a new funding print.
        self.funding: Optional[float] = None

    def touch(self) -> None:
        self.alive_ts = time.time()

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False

    # ---- zkLighter snapshot + diff ----
    def apply_lighter(self, ob: dict, snapshot: bool) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        for name, side in (("bids", self.bids), ("asks", self.asks)):
            for lvl in ob.get(name) or []:
                px, sz = float(lvl["price"]), float(lvl["size"])
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
        self.ready = True
        self.last_update_ts = time.time()
        self.touch()

    # ---- Hyperliquid full snapshot ----
    def apply_hl(self, levels: list) -> None:
        self.bids = {float(l["px"]): float(l["sz"])
                     for l in levels[0] if float(l["sz"]) > 0}
        self.asks = {float(l["px"]): float(l["sz"])
                     for l in levels[1] if float(l["sz"]) > 0}
        self.ready = True
        self.last_update_ts = time.time()
        self.touch()

    def sorted_bids(self) -> List[Level]:
        return sorted(self.bids.items(), key=lambda kv: -kv[0])

    def sorted_asks(self) -> List[Level]:
        return sorted(self.asks.items())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        if not (self.bids and self.asks):
            return None
        return (max(self.bids) + min(self.asks)) / 2.0

    def is_fresh(self, max_age_sec: float) -> bool:
        return self.ready and bool(self.bids) and bool(self.asks) and (
            time.time() - self.alive_ts <= max_age_sec)


def floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 12)


def ceil_step(x: float, step: float) -> float:
    """Smallest multiple of ``step`` that is >= ``x``."""
    if step <= 0:
        return float(x)
    return round(math.ceil(float(x) / float(step) - 1e-12) * step, 12)


def truncate_size(qty: float, decimals: int) -> float:
    """Floor ``qty`` to a venue's szDecimals.

    Hyperliquid and Lighter both send an integer multiple of
    ``10 ** -szDecimals``. Rounding half-up can look like it cleared $10
    and then truncate back under the minimum on the wire.
    """
    decimals = int(decimals)
    if decimals < 0:
        return float(qty)
    scale = Decimal(1).scaleb(-decimals)
    # ceil_step already snaps to 12 places. Round here too so a binary
    # 0.006799999999 does not floor to the previous size tick.
    rounded = format(round(float(qty), 12), "f")
    quantized = Decimal(rounded).quantize(scale, rounding=ROUND_DOWN)
    return float(quantized)


# Hyperliquid rejects ``Order must have minimum value of $10``. A quote of
# $10.00 truncates under that after szDecimals, so a probe aims here.
QUOTE_CLEAR_USD = 10.50


def sync_size_grid(entropy, hedge, min_order_notional: float):
    """Shared step and minimums for a dual-leg slice.

    The step is the coarser of the two venues' size decimals. Both the
    strategy engine and the probe's live executor use this so they cannot
    drift onto different grids.
    """
    decimals = min(int(entropy.size_decimals), int(hedge.size_decimals))
    step = 10.0 ** -decimals
    min_base = max(float(entropy.min_base), float(hedge.min_base), step)
    min_notional = max(float(min_order_notional),
                       float(entropy.min_quote), float(hedge.min_quote))
    return step, min_base, min_notional


def size_above_min_notional(
        qty: float,
        legs: Sequence[Tuple[float, int]],
        *,
        min_notional: float,
        size_step: float,
        target_notional: Optional[float] = None,
) -> Tuple[Optional[float], str]:
    """Ceil ``qty`` so every leg's truncated quote clears the minimum.

    ``legs[0]`` is Entropy as ``(limit_price, sz_decimals)``. The other
    tuple is the hedge. When Entropy would still print under its minimum,
    the result is ``(None, "below_min_notional")`` and the caller must not
    send the hedge leg.

    A probe passes ``target_notional`` (about $10.50–$20). The wire quote
    is ceiled to that target so szDecimals truncation cannot land under
    $10. Without a target, a size that already clears is kept; only a
    sub-minimum size is lifted, and only when the lift stays within one
    grid step of the floor.
    """
    if size_step <= 0 or not legs:
        return None, "below_min_notional"
    prices: List[float] = []
    for price, _dec in legs:
        price = float(price)
        if not math.isfinite(price) or price <= 0:
            return None, "below_min_notional"
        prices.append(price)

    venue_min = max(float(min_notional), 0.0)
    clear = venue_min
    if venue_min + 1e-9 >= 10.0:
        clear = max(venue_min, QUOTE_CLEAR_USD)
    if target_notional is not None:
        clear = max(clear, float(target_notional))
    if clear <= 0:
        return None, "below_min_notional"

    step = float(size_step)
    for _px, dec in legs:
        dec = int(dec)
        if dec >= 0:
            step = max(step, 10.0 ** -dec)
    step_ntl = step * max(prices)
    # A coarse grid (whole coins on a $100 name) cannot sneak up to $11.
    # Refuse that jump instead of sending a far larger order.
    if step_ntl > max(2.0, 0.15 * clear):
        max_over = 0.50
    else:
        max_over = max(0.50, step_ntl)

    ent_floor = 10.0 if venue_min + 1e-9 >= 10.0 else venue_min
    ent_floor = max(venue_min, ent_floor)

    def quotes(q: float) -> List[float]:
        return [truncate_size(q, int(dec)) * price
                for price, dec in legs]

    def clears(q: float) -> bool:
        qs = quotes(q)
        if not qs or qs[0] + 1e-6 < ent_floor:
            return False
        return all(v + 1e-6 >= clear for v in qs)

    start = float(qty) if qty and qty > 0 else 0.0
    if start > 0 and clears(start):
        return start, "ok"

    raw = max(clear / px for px in prices)
    q = ceil_step(max(start, raw), step)
    for _ in range(8):
        qs = quotes(q)
        if clears(q):
            if max(qs) > clear + max_over + 1e-6:
                return None, "below_min_notional"
            return q, "ok"
        if qs and min(qs) + 1e-6 >= clear and max(qs) > clear + max_over:
            return None, "below_min_notional"
        nxt = ceil_step(q + step, step)
        if nxt <= q + 1e-15:
            break
        q = nxt
    return None, "below_min_notional"


def crossable_base(asks: List[Level], bids: List[Level], threshold: float,
                   buy_fee: float = 0.0, sell_fee: float = 0.0) -> Tuple[float, float]:
    """Walk both books level by level and return (base qty, buy notional) that
    can be crossed while every marginal slice still clears fees + threshold."""
    qty = 0.0
    buy_notional = 0.0
    i = j = 0
    a_px = a_rem = 0.0
    b_px = b_rem = 0.0
    while True:
        if a_rem <= 0:
            if i >= len(asks):
                break
            a_px, a_rem = asks[i]
            i += 1
        if b_rem <= 0:
            if j >= len(bids):
                break
            b_px, b_rem = bids[j]
            j += 1
        if b_px * (1.0 - sell_fee) < a_px * (1.0 + buy_fee) * (1.0 + threshold):
            break
        take = min(a_rem, b_rem)
        qty += take
        buy_notional += take * a_px
        a_rem -= take
        b_rem -= take
    return qty, buy_notional


def walk_depth(levels: List[Level], qty: float) -> Tuple[float, float]:
    remaining = qty
    notional = 0.0
    marginal_px = levels[0][0]
    for px, sz in levels:
        take = min(remaining, sz)
        notional += take * px
        marginal_px = px
        remaining -= take
        if remaining <= 1e-12:
            break
    return marginal_px, notional


@dataclass
class ArbPlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    q_max: float
    q_max_notional: float
    top_premium_bps: float
    marginal_premium_bps: float
    buy_fee: float
    sell_fee: float

    @property
    def gross_edge_usd(self) -> float:
        return self.sell_notional - self.buy_notional

    @property
    def exp_edge_usd(self) -> float:
        return (self.sell_notional * (1.0 - self.sell_fee)
                - self.buy_notional * (1.0 + self.buy_fee))


def plan_arb(buy_book: OrderBook, sell_book: OrderBook, *, threshold_bps: float,
             buy_fee_bps: float, sell_fee_bps: float, take_fraction: float,
             cap_notional: float, min_base: float, min_notional: float,
             size_step: float):
    """Size a two-leg taker slice: buy on buy_book, sell on sell_book.

    A slice qualifies when the executable premium (sell bid over buy ask)
    clears both venues' taker fees plus threshold_bps. Returns
    (ArbPlan | None, reason).
    """
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    threshold = threshold_bps / 1e4
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    if bids[0][0] * (1.0 - sell_fee) < asks[0][0] * (1.0 + buy_fee) * (1.0 + threshold):
        return None, "no_edge"
    q_max, q_max_notional = crossable_base(asks, bids, threshold, buy_fee, sell_fee)
    if q_max <= 0:
        return None, "no_edge"
    target = min(q_max * take_fraction, cap_notional / asks[0][0])
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if buy_notional < min_notional or sell_notional < min_notional:
        # floor(cap / price) can print $9.96 on a $10 minimum. Ceil onto
        # the size grid when the book can hold that one step.
        px = min(asks[0][0], bids[0][0])
        if px <= 0 or size_step <= 0:
            return None, "below_min_notional"
        lifted = ceil_step(min_notional / px, size_step)
        if lifted < min_base or lifted > q_max + 1e-12:
            return None, "below_min_notional"
        step_ntl = size_step * max(asks[0][0], bids[0][0])
        ceiling = max(cap_notional, min_notional) + max(step_ntl, 0.5)
        buy_limit, buy_notional = walk_depth(asks, lifted)
        sell_limit, sell_notional = walk_depth(bids, lifted)
        if (buy_notional > ceiling + 1e-6 or sell_notional > ceiling + 1e-6
                or buy_notional < min_notional or sell_notional < min_notional):
            return None, "below_min_notional"
        target = lifted
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee,
    ), "ok"
