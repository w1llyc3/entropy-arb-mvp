"""Place one admitted dual-leg probe through the CLI engine.

The panel recorder stays ``--record-only``. This module is the only order
path the confirm button uses, and it calls ``Engine.execute_confirmed``,
which sends both legs with ``send_taker``. A blank confirm id never reaches
a venue.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import aiohttp

from entropy_arb.config import load_config
from entropy_arb.engine import Engine
from web.accounts import missing_live_env

_BOOK_WAIT_SEC = 12.0


def execute_admitted(req: dict) -> dict:
    """Synchronous entry used by the panel. Refuses before any network if
    the confirm id or the $10 cap is missing."""
    confirm_id = str((req or {}).get("confirm_id") or "").strip()
    if not confirm_id:
        raise RuntimeError("refusing order without an admitted confirm id")
    order = float(req.get("order_notional_usd") or 0.0)
    position = float(req.get("max_position_usd") or 0.0)
    if order - 10.0 > 1e-9 or position - 10.0 > 1e-9:
        raise RuntimeError("probe caps are $10")
    return asyncio.run(_route(req, confirm_id))


async def _route(req: dict, confirm_id: str) -> dict:
    root = Path(req["root"])
    if missing_live_env(root):
        return _idle(confirm_id, "credentials incomplete")
    cfg_path = root / ".web" / "probe.yaml"
    env_path = root / ".env"
    cfg = load_config(str(cfg_path), str(env_path),
                      symbol="SNDK", hedge_venue="lighter")
    cfg.max_order_notional = min(float(cfg.max_order_notional), 10.0)
    cfg.entropy.cap_usd = min(float(cfg.entropy.cap_usd), 10.0)
    cfg.hedge.cap_usd = min(float(cfg.hedge.cap_usd), 10.0)
    eng = Engine(cfg, record_only=False)
    eng.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
        keepalive_timeout=75.0, ttl_dns_cache=300))
    tasks = []
    try:
        eng.entropy = eng._make_venue(cfg.entropy)
        eng.hedge = eng._make_venue(cfg.hedge)
        eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
        await asyncio.gather(eng.entropy.load_market(), eng.hedge.load_market())
        if not cfg.creds_complete:
            return _idle(confirm_id, "credentials incomplete")
        eng.entropy.init_signer()
        eng.hedge.init_signer()
        eng._step = 10 ** -min(eng.entropy.size_decimals, eng.hedge.size_decimals)
        eng._min_base = max(eng.entropy.min_base, eng.hedge.min_base, eng._step)
        eng._min_notional = max(cfg.min_order_notional,
                                eng.entropy.min_quote, eng.hedge.min_quote)
        for venue in eng.venues.values():
            tasks += venue.start_tasks(eng.stop, eng._update_evt.set, True)
        fresh = await _wait_books(eng)
        if not fresh:
            return _idle(confirm_id, "books not fresh")
        result = await eng.execute_confirmed(
            direction=str(req.get("direction") or ""),
            confirm_id=confirm_id,
            cap_notional=min(10.0, float(req.get("order_notional_usd") or 10.0)),
        )
        if result.get("sent") and result.get("entropy_fee_bps") is None:
            fee = await _entropy_fee(eng, result)
            if fee is not None:
                result["entropy_fee_bps"] = fee
        return result
    finally:
        eng.request_stop()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for venue in (eng.venues or {}).values():
            closer = getattr(venue, "close", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                pass
        await eng.session.close()


async def _wait_books(eng: Engine) -> bool:
    deadline = time.time() + _BOOK_WAIT_SEC
    while time.time() < deadline:
        ready = True
        for venue in eng.venues.values():
            if not venue.book.is_fresh(eng.cfg.staleness_sec):
                ready = False
            if not venue.ready_to_trade():
                ready = False
        if ready:
            return True
        await asyncio.sleep(0.2)
    return False


async def _entropy_fee(eng: Engine, result: dict):
    """Read the Entropy leg fee from the venue that already filled, if it can."""
    venue = eng.entropy
    reader = getattr(venue, "recent_fill_fee_bps", None)
    if reader is None:
        return None
    try:
        return await reader()
    except Exception:
        return None


def _idle(confirm_id: str, error: str) -> dict:
    return {
        "ok": False, "routed": False, "sent": False, "halted": False,
        "confirm_id": confirm_id, "error": error,
        "buy_fill": 0.0, "sell_fill": 0.0, "net_base": 0.0,
        "entropy_fee_bps": None, "status": error,
    }
