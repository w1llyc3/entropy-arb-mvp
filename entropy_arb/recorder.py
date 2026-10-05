"""Automatic 1-minute orderbook data recorder.

While the bot runs (live or --record-only), both venues' actual order books
are sampled once per second and aggregated into one CSV row per minute.
This is the dataset users analyze (tools/analyze.py) to choose
thresholds.midline_bps / upper_bps / lower_bps for config.yaml.

Definitions (all in bps, fees NOT included — the engine adds fees on top):

    premium    = (entropy_mid / hedge_mid - 1) * 1e4
                 the mid-to-mid premium of Entropy over the hedge venue;
                 its long-run center is what midline_bps hardcodes.
    sell_edge  = (entropy_bid / hedge_ask - 1) * 1e4
                 the EXECUTABLE premium for SELL-entropy/BUY-hedge; the
                 engine fires this direction when sell_edge clears
                 midline_bps + upper_bps (plus fees).
    buy_edge   = (hedge_bid / entropy_ask - 1) * 1e4
                 the executable premium for BUY-entropy/SELL-hedge; fires
                 when buy_edge clears lower_bps - midline_bps (plus fees).

Fillable columns (also pre-fee) walk both books to a target USD notional
with the existing helpers ``walk_depth`` and ``crossable_base``:

    fill_{sell,buy}_edge_{50,100,250}_bps
        marginal executable edge after a matched-qty walk that puts at
        least that many dollars through BOTH legs. Empty when either book
        is too thin for the notional.
    slip_{sell,buy}_{50,100,250}_bps
        top-of-book edge minus the average-fill edge at that notional
        (positive = worse than the touch). Empty when depth is insufficient.
    depth_ok_frac
        fraction of this minute's fresh samples where BOTH directions can
        fill >= $100 notional.
    entropy_funding, hedge_funding
        last sample's funding rate if the book exposes a numeric ``funding``
        attribute. The upstream feeds do not, so these stay blank. Nothing
        is invented.

Aggregation: each 1 Hz sample computes the fillable metrics; the minute
cell is the arithmetic mean of samples that could fill that notional.
(A last-sample snapshot would hide intra-minute depth gaps; the walks are
cheap at 1 Hz.) ``depth_ok_frac`` uses every fresh sample as the denominator.

Bid/ask columns are the minute's last fresh sample (close). A row is only
written for minutes with at least one sample where both books were fresh;
``samples`` says how many of the ~60 seconds qualified.

An existing CSV whose header does not match ``HEADER`` is rotated to
``<path>.old`` before appending, so an older schema is never mixed in.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .book import Level, OrderBook, crossable_base, walk_depth

log = logging.getLogger("recorder")

FILL_NOTIONALS = (50.0, 100.0, 250.0)
DEPTH_OK_NOTIONAL = 100.0

_TOB = ["minute_ts", "time_utc",
        "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
        "premium_open_bps", "premium_high_bps", "premium_low_bps",
        "premium_close_bps", "premium_mean_bps", "premium_std_bps",
        "sell_edge_mean_bps", "sell_edge_max_bps",
        "buy_edge_mean_bps", "buy_edge_max_bps", "samples"]


def _extra_header() -> List[str]:
    cols: List[str] = []
    for side in ("sell", "buy"):
        for n in (50, 100, 250):
            cols.append(f"fill_{side}_edge_{n}_bps")
    for side in ("sell", "buy"):
        for n in (50, 100, 250):
            cols.append(f"slip_{side}_{n}_bps")
    cols += ["depth_ok_frac", "entropy_funding", "hedge_funding"]
    return cols


HEADER = _TOB + _extra_header()


def _qty_for_notional(levels: List[Level], target_usd: float) -> Optional[float]:
    """Base qty whose ``walk_depth`` notional reaches ``target_usd``, or None."""
    if target_usd <= 0 or not levels:
        return None
    total = 0.0
    for px, sz in levels:
        if px > 0 and sz > 0:
            total += sz
    if total <= 0:
        return None
    _, full = walk_depth(levels, total)
    if full + 1e-6 < target_usd:
        return None
    lo, hi = 0.0, total
    for _ in range(48):
        mid = (lo + hi) * 0.5
        _, ntl = walk_depth(levels, mid)
        if ntl >= target_usd:
            hi = mid
        else:
            lo = mid
    _, ntl = walk_depth(levels, hi)
    if ntl + 1e-4 < target_usd:
        return None
    return hi


def fillable_edge(sell_levels: List[Level], buy_levels: List[Level],
                  target_usd: float) -> Tuple[Optional[float], Optional[float]]:
    """Marginal pre-fee edge and TOB-vs-average slip at ``target_usd``.

    ``sell_levels`` are the bids we hit, ``buy_levels`` the asks we lift,
    each best-first. Both legs trade one matched base qty large enough that
    each leg's notional is at least ``target_usd``. Returns ``(None, None)``
    when either book cannot support that size. Slip is top-of-book edge
    minus average-fill edge, in bps (positive means the walk is worse than
    the touch).
    """
    if not sell_levels or not buy_levels:
        return None, None
    if sell_levels[0][0] <= 0 or buy_levels[0][0] <= 0:
        return None, None
    # threshold -100% so the premium check never stops the walk; the helper
    # then reports the matched base the two books can actually exchange.
    q_match, _ = crossable_base(buy_levels, sell_levels, threshold=-1.0)
    if q_match <= 0:
        return None, None
    q_sell = _qty_for_notional(sell_levels, target_usd)
    q_buy = _qty_for_notional(buy_levels, target_usd)
    if q_sell is None or q_buy is None:
        return None, None
    qty = max(q_sell, q_buy)
    if qty > q_match + 1e-8:
        return None, None
    sell_marg, sell_ntl = walk_depth(sell_levels, qty)
    buy_marg, buy_ntl = walk_depth(buy_levels, qty)
    if (sell_ntl + 1e-4 < target_usd or buy_ntl + 1e-4 < target_usd
            or qty <= 0 or sell_marg <= 0 or buy_marg <= 0):
        return None, None
    marginal_edge = (sell_marg / buy_marg - 1.0) * 1e4
    avg_edge = ((sell_ntl / qty) / (buy_ntl / qty) - 1.0) * 1e4
    tob_edge = (sell_levels[0][0] / buy_levels[0][0] - 1.0) * 1e4
    return marginal_edge, tob_edge - avg_edge


def _read_funding(book: OrderBook) -> Optional[float]:
    """Numeric funding already hanging off the book, or None. Never invented."""
    raw = getattr(book, "funding", None)
    if callable(raw):
        try:
            raw = raw()
        except Exception:
            return None
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def _fmt_mean(total: float, n: int, digits: int = 3) -> str:
    if n <= 0:
        return ""
    return f"{total / n:.{digits}f}"


def _fmt_funding(val: Optional[float]) -> str:
    if val is None:
        return ""
    return f"{val:.8g}"


class _MinuteAgg:
    __slots__ = ("minute", "n", "p_open", "p_high", "p_low", "p_close",
                 "p_sum", "p_sumsq", "s_sum", "s_max", "b_sum", "b_max",
                 "e_bid", "e_ask", "h_bid", "h_ask",
                 "fill_sum", "fill_n", "slip_sum", "slip_n", "depth_ok",
                 "e_funding", "h_funding")

    def __init__(self, minute: int) -> None:
        self.minute = minute
        self.n = 0
        self.p_open = self.p_high = self.p_low = self.p_close = 0.0
        self.p_sum = self.p_sumsq = 0.0
        self.s_sum = 0.0
        self.s_max = -math.inf
        self.b_sum = 0.0
        self.b_max = -math.inf
        self.e_bid = self.e_ask = self.h_bid = self.h_ask = 0.0
        self.fill_sum = {}
        self.fill_n = {}
        self.slip_sum = {}
        self.slip_n = {}
        for side in ("sell", "buy"):
            for ntl in FILL_NOTIONALS:
                self.fill_sum[(side, ntl)] = 0.0
                self.fill_n[(side, ntl)] = 0
                self.slip_sum[(side, ntl)] = 0.0
                self.slip_n[(side, ntl)] = 0
        self.depth_ok = 0
        self.e_funding: Optional[float] = None
        self.h_funding: Optional[float] = None

    def add(self, e_bids: List[Level], e_asks: List[Level],
            h_bids: List[Level], h_asks: List[Level],
            e_funding: Optional[float] = None,
            h_funding: Optional[float] = None) -> None:
        e_bid, e_ask = e_bids[0][0], e_asks[0][0]
        h_bid, h_ask = h_bids[0][0], h_asks[0][0]
        e_mid = (e_bid + e_ask) / 2.0
        h_mid = (h_bid + h_ask) / 2.0
        prem = (e_mid / h_mid - 1.0) * 1e4
        sell_edge = (e_bid / h_ask - 1.0) * 1e4
        buy_edge = (h_bid / e_ask - 1.0) * 1e4
        if self.n == 0:
            self.p_open = self.p_high = self.p_low = prem
        self.n += 1
        self.p_high = max(self.p_high, prem)
        self.p_low = min(self.p_low, prem)
        self.p_close = prem
        self.p_sum += prem
        self.p_sumsq += prem * prem
        self.s_sum += sell_edge
        self.s_max = max(self.s_max, sell_edge)
        self.b_sum += buy_edge
        self.b_max = max(self.b_max, buy_edge)
        self.e_bid, self.e_ask, self.h_bid, self.h_ask = e_bid, e_ask, h_bid, h_ask

        sides = {
            "sell": (e_bids, h_asks),   # sell entropy, buy hedge
            "buy": (h_bids, e_asks),    # sell hedge, buy entropy
        }
        depth_ok = True
        for side, (sell_lv, buy_lv) in sides.items():
            for ntl in FILL_NOTIONALS:
                edge, slip = fillable_edge(sell_lv, buy_lv, ntl)
                if edge is None or slip is None:
                    if ntl == DEPTH_OK_NOTIONAL:
                        depth_ok = False
                    continue
                self.fill_sum[(side, ntl)] += edge
                self.fill_n[(side, ntl)] += 1
                self.slip_sum[(side, ntl)] += slip
                self.slip_n[(side, ntl)] += 1
        if depth_ok:
            self.depth_ok += 1
        if e_funding is not None:
            self.e_funding = e_funding
        if h_funding is not None:
            self.h_funding = h_funding

    def row(self) -> list:
        mean = self.p_sum / self.n
        var = max(self.p_sumsq / self.n - mean * mean, 0.0)
        ts = self.minute * 60
        out = [ts,
               datetime.fromtimestamp(ts, tz=timezone.utc)
               .strftime("%Y-%m-%dT%H:%M:%SZ"),
               f"{self.e_bid:.10g}", f"{self.e_ask:.10g}",
               f"{self.h_bid:.10g}", f"{self.h_ask:.10g}",
               f"{self.p_open:.3f}", f"{self.p_high:.3f}",
               f"{self.p_low:.3f}", f"{self.p_close:.3f}",
               f"{mean:.3f}", f"{math.sqrt(var):.3f}",
               f"{self.s_sum / self.n:.3f}", f"{self.s_max:.3f}",
               f"{self.b_sum / self.n:.3f}", f"{self.b_max:.3f}",
               self.n]
        for side in ("sell", "buy"):
            for ntl in FILL_NOTIONALS:
                out.append(_fmt_mean(self.fill_sum[(side, ntl)],
                                     self.fill_n[(side, ntl)]))
        for side in ("sell", "buy"):
            for ntl in FILL_NOTIONALS:
                out.append(_fmt_mean(self.slip_sum[(side, ntl)],
                                     self.slip_n[(side, ntl)]))
        out.append(f"{self.depth_ok / self.n:.4f}")
        out.append(_fmt_funding(self.e_funding))
        out.append(_fmt_funding(self.h_funding))
        return out


class MinuteRecorder:
    def __init__(self, path: str, entropy_book: OrderBook, hedge_book: OrderBook,
                 staleness_sec: float, interval_sec: float = 1.0) -> None:
        self.path = path
        self.entropy_book = entropy_book
        self.hedge_book = hedge_book
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.rows_written = 0
        self._agg: Optional[_MinuteAgg] = None
        self._fh = None
        self._writer = None

    def _open(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            # never append rows under a different schema's header
            with open(self.path) as fh0:
                if fh0.readline().strip() != ",".join(HEADER):
                    log.warning("%s has an old header — rotated to %s.old",
                                self.path, self.path)
                    os.replace(self.path, self.path + ".old")
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(HEADER)
            self._fh.flush()
        log.info("recording 1-minute orderbook data -> %s", self.path)

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        if self._writer is None:
            self._open()
        self._writer.writerow(self._agg.row())
        self._fh.flush()
        self.rows_written += 1
        self._agg = None

    def sample(self, now: Optional[float] = None) -> None:
        """Take one sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not (self.entropy_book.is_fresh(self.staleness_sec)
                and self.hedge_book.is_fresh(self.staleness_sec)):
            return
        e_bids, e_asks = (self.entropy_book.sorted_bids(),
                          self.entropy_book.sorted_asks())
        h_bids, h_asks = (self.hedge_book.sorted_bids(),
                          self.hedge_book.sorted_asks())
        if not (e_bids and e_asks and h_bids and h_asks):
            return
        if self._agg is None:
            self._agg = _MinuteAgg(minute)
        self._agg.add(e_bids, e_asks, h_bids, h_asks,
                      _read_funding(self.entropy_book),
                      _read_funding(self.hedge_book))

    def close(self) -> None:
        """Flush the partial minute and close the file (call on shutdown)."""
        self._flush_agg()
        if self._fh is not None:
            self._fh.close()
            self._fh = self._writer = None

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("recorder sample failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.close()
            log.info("recorder stopped — %d minute row(s) written to %s",
                     self.rows_written, self.path)
