"""Record-only funding polls. No keys, no orders.

Book websockets do not carry funding, so the recorder leaves
``entropy_funding`` / ``hedge_funding`` blank unless something numeric is
already on the book. This module fills that attribute from public REST and
never invents a rate.

Endpoints (public, documented):

* Entropy / Hyperliquid HIP-3 — ``POST https://api.hyperliquid.xyz/info``
  body ``{"type": "metaAndAssetCtxs", "dex": "<dex>"}``. For Entropy the dex
  is ``io``. The matching asset context's ``funding`` field is the current
  funding rate and is stored unscaled.
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals

* Lighter — ``GET {profile}/api/v1/funding-rates``. On mainnet the host is
  ``https://mainnet.zklighter.elliot.ai``. The row with ``exchange`` equal to
  ``lighter`` and this market's ``market_id`` supplies ``rate``, stored
  unscaled. Rows for binance, bybit, and hyperliquid in the same payload are
  not Lighter's own rate and are ignored.
  https://apidocs.lighter.xyz/reference/funding-rates

A failed poll logs the reason. The CSV cell stays blank until the first
success. A later failure does not erase the last value that came from a
successful response.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

log = logging.getLogger("funding")

FUNDING_POLL_SEC = 60.0


def _finite(raw) -> Optional[float]:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def parse_hl_funding(body, dex: str, symbol: str) -> tuple[Optional[float], Optional[str]]:
    """Pull ``funding`` for ``dex:symbol`` out of a metaAndAssetCtxs body.

    Returns ``(rate, None)`` or ``(None, reason)``. ``reason`` is logged; the
    caller must not substitute a number.
    """
    if not isinstance(body, list) or len(body) < 2:
        return None, "metaAndAssetCtxs response was not [meta, assetCtxs]"
    meta, ctxs = body[0], body[1]
    if not isinstance(meta, dict) or not isinstance(ctxs, list):
        return None, "metaAndAssetCtxs response shape mismatch"
    universe = meta.get("universe")
    if not isinstance(universe, list):
        return None, "metaAndAssetCtxs meta.universe missing"
    want = {f"{dex}:{symbol}", symbol}
    for idx, asset in enumerate(universe):
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if name not in want:
            continue
        if idx >= len(ctxs) or not isinstance(ctxs[idx], dict):
            return None, f"no assetCtx aligned with {name}"
        raw = ctxs[idx].get("funding")
        if raw is None or raw == "":
            return None, f"funding field blank for {name}"
        val = _finite(raw)
        if val is None:
            return None, f"funding not a finite number for {name}: {raw!r}"
        return val, None
    return None, f"{dex}:{symbol} not in metaAndAssetCtxs universe"


def parse_lighter_funding(body, symbol: str,
                          market_id: Optional[int] = None
                          ) -> tuple[Optional[float], Optional[str]]:
    """Pull Lighter's own ``rate`` from a ``/api/v1/funding-rates`` body.

    When ``market_id`` is set it must match. Otherwise the row is chosen by
    symbol. Other exchanges in the payload are ignored.
    """
    if not isinstance(body, dict):
        return None, "funding-rates response was not a JSON object"
    code = body.get("code")
    if code not in (None, 200):
        return None, (f"funding-rates code={code} "
                      f"message={body.get('message')!r}")
    rows = body.get("funding_rates")
    if not isinstance(rows, list):
        return None, "funding-rates missing funding_rates array"
    own = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("exchange", "")).lower() != "lighter":
            continue
        own.append(row)
    chosen = None
    if market_id is not None:
        for row in own:
            try:
                if int(row.get("market_id")) == int(market_id):
                    chosen = row
                    break
            except (TypeError, ValueError):
                continue
        if chosen is None:
            return None, (f"no exchange=lighter funding-rates row for "
                          f"market_id={market_id}")
    else:
        want = symbol.upper()
        for row in own:
            if str(row.get("symbol", "")).upper() == want:
                chosen = row
                break
        if chosen is None:
            return None, f"no exchange=lighter funding-rates row for symbol={symbol}"
    raw = chosen.get("rate")
    val = _finite(raw)
    if val is None:
        return None, f"lighter funding rate not a finite number: {raw!r}"
    return val, None


class FundingPoller:
    """Poll both venues on a timer and store real rates on their books."""

    def __init__(self, entropy, hedge, interval_sec: float = FUNDING_POLL_SEC) -> None:
        self.entropy = entropy
        self.hedge = hedge
        self.interval_sec = interval_sec
        self._announced: dict = {}

    async def poll_once(self) -> None:
        await self._poll_one("entropy", self.entropy)
        await self._poll_one("hedge", self.hedge)

    async def _poll_one(self, label: str, venue) -> None:
        book = getattr(venue, "book", None)
        fetch = getattr(venue, "fetch_funding_rate", None)
        if book is None or fetch is None:
            self._note_failure(label, book,
                               "no documented public funding endpoint on this venue")
            return
        try:
            rate, err = await fetch()
        except Exception as exc:
            rate, err = None, f"{type(exc).__name__}: {exc}"
        if err or rate is None:
            self._note_failure(label, book, err or "endpoint returned no numeric funding")
            return
        book.funding = rate
        key = ("ok", rate)
        if self._announced.get(label) != key:
            log.info("%s funding %.8g", label, rate)
            self._announced[label] = key

    def _note_failure(self, label: str, book, why: str) -> None:
        prior = getattr(book, "funding", None) if book is not None else None
        if prior is None:
            log.warning("%s funding left blank: %s", label, why)
        else:
            log.warning("%s funding poll failed (%s); keeping last fetched value",
                        label, why)
        self._announced[label] = ("err", why)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.poll_once()
                except Exception:
                    log.exception("funding poll failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            log.info("funding poller stopped")
