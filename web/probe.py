"""SNDK Entropy ↔ Lighter probe: Decision Card, RTH gate, confirm payload.

Pure functions. No network and no secrets. Order routing lives in
``web.live_exec`` and only runs after an admitted confirm id.
"""
from __future__ import annotations

import math
from datetime import datetime, time as dtime, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from entropy_arb.config import display_accrual_bps
from tools.analyze import assign_session

# Locked Decision Card (Researcher). The create form opens on these values.
DECISION_MIDLINE_BPS = -1.7
DECISION_UPPER_BPS = 1.0
DECISION_LOWER_BPS = 1.0
DECISION_ORDER_USD = 10.0
DECISION_POSITION_USD = 10.0
ENTROPY_FEE_BPS = 0.9
LIGHTER_FEE_BPS = 0.0
SYMBOL = "SNDK"
ENTROPY_DEX = "io"
HEDGE = "lighter"
REFERRAL_MODE = "self_t2"
PERSIST_SEC = 3.0
MIN_ORDER_USD = 10.0
# Entropy taker open + taker close. Lighter Standard is ~0, so the round
# trip is 1.8 bps unless a fill has measured a different Entropy fee.
# Auto-confirm must use this, never the one-way 0.9 bps open fee.
ROUND_TRIP_OPEN_CLOSE = 2
MARGIN_SAFETY = 0.80
# Opposite-leg sizes within this fraction (or NET_TOL_BASE, whichever is
# larger) count as one arb. A one-leg residual is a HALT, not a close.
SYM_REL_TOL = 0.02

# Gross Tier2-self accrual on the 0.9 bps fee. Not cash. Not added to net edge.
# 0.9 * 0.50 * 1.20 = 0.54 bps. Rounded so the confirm card shows 0.54.
ACCRUAL_BPS = round(display_accrual_bps(ENTROPY_FEE_BPS, REFERRAL_MODE), 4)
ACCRUAL_LABEL = "未到账"
DECISION_WARNING = "会偏离 Decision Card"

# Measured session midlines. Display only — the task midline never follows them.
SESSION_MIDLINE_BPS = {
    "us_regular": -1.7,
    "us_post_overnight": 2.6,
    "asia": -0.4,
}
FORCE_RISK_LINE = "带宽 1 bps，错中枢风险大于费率缺口"
FEE_MISMATCH_TOL_BPS = 0.05
NET_TOL_BASE = 0.001

RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)
RTH_WINDOW = "America/New_York 09:30–16:00"

# Loaded on first RTH check. Constructing this at import crashes
# ``python -m web`` on Windows when the tzdata package is absent.
_NY: Optional[ZoneInfo] = None

# Weekday session only. Exchange holidays are not on this calendar.
CONFIRM_FIELDS = (
    "proposal_id",
    "net_edge_bps",
    "fee_bps",
    "accrual_bps",
    "accrual_label",
    "referral_mode",
    "midline_bps",
    "rth",
    "legs",
    "tail_vs_median",
    "confirm",
    "intent",
)
LEG_FIELDS = ("venue", "direction", "notional_usd", "available", "isolated")

# Status pills the task card is allowed to show.
STATUS_RECORDING = "记录中"
STATUS_WARMING = "暖机"
STATUS_LIVE = "LIVE"
STATUS_PAUSED = "暂停"
STATUS_STOPPED = "已停止"
STATUS_HALT = "HALT"


def decision_defaults() -> dict:
    """Create-form defaults. Fees and the referral line are read-only."""
    return {
        "symbol": SYMBOL,
        "entropy_dex": ENTROPY_DEX,
        "hedge": HEDGE,
        "hedge_label": "Lighter",
        "midline_bps": DECISION_MIDLINE_BPS,
        "upper_bps": DECISION_UPPER_BPS,
        "lower_bps": DECISION_LOWER_BPS,
        "order_notional_usd": DECISION_ORDER_USD,
        "max_position_usd": DECISION_POSITION_USD,
        "entropy_fee_bps": ENTROPY_FEE_BPS,
        "lighter_fee_bps": LIGHTER_FEE_BPS,
        "referral_mode": REFERRAL_MODE,
        "accrual_bps": ACCRUAL_BPS,
        "accrual_label": ACCRUAL_LABEL,
        "persist_sec": PERSIST_SEC,
        "manual_confirm": True,
        "auto_confirm": False,
        "auto_confirm_max_usd": DECISION_ORDER_USD,
        "auto_daily_max_notional_usd": None,
        "auto_daily_max_count": None,
        "sizing_mode": "cash",
        "margin_safety": MARGIN_SAFETY,
        "rth_only": True,
        "mode": "record",
    }


def _optional_cap(raw: dict, name: str) -> Optional[float]:
    """Blank, null, or <= 0 means the optional daily cap is off."""
    if name not in raw or raw.get(name) in (None, ""):
        return None
    number = _num(raw.get(name), name)
    if number <= 0:
        return None
    return number


def _num(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def normalize_task(body: Optional[dict]) -> dict:
    """One SNDK task. Symbol, dex, hedge, and fees are not caller-selectable."""
    raw = body or {}
    if not isinstance(raw, dict):
        raise ValueError("task body must be an object")
    for key in ("symbol", "entropy_dex", "hedge"):
        if key in raw and raw[key] not in (None, "", decision_defaults()[key]):
            raise ValueError(
                f"{key} is locked to the SNDK Entropy ↔ Lighter probe")
    spec = decision_defaults()
    spec["midline_bps"] = _num(raw.get("midline_bps", spec["midline_bps"]),
                               "midline_bps")
    spec["upper_bps"] = _num(raw.get("upper_bps", spec["upper_bps"]),
                             "upper_bps")
    spec["lower_bps"] = _num(raw.get("lower_bps", spec["lower_bps"]),
                             "lower_bps")
    spec["order_notional_usd"] = _num(
        raw.get("order_notional_usd", spec["order_notional_usd"]),
        "order_notional_usd")
    spec["max_position_usd"] = _num(
        raw.get("max_position_usd", spec["max_position_usd"]),
        "max_position_usd")
    if spec["upper_bps"] <= 0 or spec["lower_bps"] <= 0:
        raise ValueError("upper_bps and lower_bps must be > 0")
    if spec["order_notional_usd"] < MIN_ORDER_USD:
        raise ValueError(
            f"order_notional_usd must be >= {MIN_ORDER_USD:.0f}")
    if spec["order_notional_usd"] - DECISION_ORDER_USD > 1e-9:
        raise ValueError("order_notional_usd cap is $10")
    if spec["max_position_usd"] - DECISION_POSITION_USD > 1e-9:
        raise ValueError("max_position_usd cap is $10")
    if spec["max_position_usd"] < spec["order_notional_usd"]:
        raise ValueError("max_position_usd must cover one order")
    mode = str(raw.get("mode", spec["mode"]) or "").strip().lower()
    if mode not in ("record", "live"):
        raise ValueError("mode must be record (只记录) or live (探针实盘)")
    spec["mode"] = mode
    if "manual_confirm" in raw and not isinstance(raw["manual_confirm"], bool):
        raise ValueError("manual_confirm must be true or false")
    if "rth_only" in raw and not isinstance(raw["rth_only"], bool):
        raise ValueError("rth_only must be true or false")
    spec["manual_confirm"] = (bool(raw["manual_confirm"])
                              if "manual_confirm" in raw else True)
    if "auto_confirm" in raw and not isinstance(raw["auto_confirm"], bool):
        raise ValueError("auto_confirm must be true or false")
    spec["auto_confirm"] = (bool(raw["auto_confirm"])
                            if "auto_confirm" in raw else False)
    if raw.get("auto_confirm_max_usd") in (None, ""):
        spec["auto_confirm_max_usd"] = spec["order_notional_usd"]
    else:
        spec["auto_confirm_max_usd"] = _num(
            raw.get("auto_confirm_max_usd"), "auto_confirm_max_usd")
    if spec["auto_confirm_max_usd"] <= 0:
        raise ValueError("auto_confirm_max_usd must be > 0")
    if spec["auto_confirm_max_usd"] - spec["order_notional_usd"] > 1e-9:
        raise ValueError(
            "auto_confirm_max_usd cannot exceed the order hard max")
    spec["auto_daily_max_notional_usd"] = _optional_cap(
        raw, "auto_daily_max_notional_usd")
    spec["auto_daily_max_count"] = _optional_cap(raw, "auto_daily_max_count")
    sizing = str(raw.get("sizing_mode", spec["sizing_mode"]) or "").strip().lower()
    if sizing not in ("cash", "margin"):
        raise ValueError("sizing_mode must be cash or margin")
    spec["sizing_mode"] = sizing
    spec["margin_safety"] = _num(
        raw.get("margin_safety", spec["margin_safety"]), "margin_safety")
    if not 0 < spec["margin_safety"] <= 1:
        raise ValueError("margin_safety must be in (0, 1]")
    spec["rth_only"] = bool(raw["rth_only"]) if "rth_only" in raw else True
    # Fees and referral stay on the Decision Card even if the body tries
    # to overwrite them.
    spec["entropy_fee_bps"] = ENTROPY_FEE_BPS
    spec["lighter_fee_bps"] = LIGHTER_FEE_BPS
    spec["referral_mode"] = REFERRAL_MODE
    spec["accrual_bps"] = ACCRUAL_BPS
    spec["accrual_label"] = ACCRUAL_LABEL
    spec["symbol"] = SYMBOL
    spec["entropy_dex"] = ENTROPY_DEX
    spec["hedge"] = HEDGE
    spec["persist_sec"] = PERSIST_SEC
    return spec


def decision_warnings(spec: dict) -> list:
    """Visible when midline leaves -1.7 or manual confirm is turned off."""
    off_mid = abs(float(spec["midline_bps"]) - DECISION_MIDLINE_BPS) > 1e-9
    confirm_off = not spec.get("manual_confirm", True)
    if off_mid or confirm_off:
        return [DECISION_WARNING]
    return []


def _new_york() -> ZoneInfo:
    """America/New_York. Windows stdlib zoneinfo needs the tzdata package."""
    global _NY
    if _NY is None:
        try:
            _NY = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError as exc:
            raise ZoneInfoNotFoundError(
                "No time zone found with key America/New_York. "
                "On Windows install the IANA database: pip install tzdata"
            ) from exc
    return _NY


def session_snapshot(now: datetime, task_midline: float) -> dict:
    """Current session and its measured midline. Does not change the task.

    Deviation is session midline minus the task midline. A task still at
    -1.7 during us_post_overnight (+2.6) is about 4 bps off.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    name = assign_session(now.timestamp())
    measured = float(SESSION_MIDLINE_BPS[name])
    delta = measured - float(task_midline)
    approx = int(round(abs(delta)))
    return {
        "name": name,
        "midline_bps": measured,
        "task_midline_bps": float(task_midline),
        "deviation_bps": round(delta, 4),
        "deviation_text": f"偏离约 {approx} bps",
        "risk_line": FORCE_RISK_LINE,
    }


def in_us_rth(now: datetime) -> bool:
    """US cash session, America/New_York, weekdays 09:30 inclusive to 16:00 exclusive.

    Saturday and Sunday are outside. NYSE holidays are not modeled.
    Naive datetimes are read as UTC.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    local = now.astimezone(_new_york())
    if local.weekday() >= 5:
        return False
    clock = local.timetz().replace(tzinfo=None)
    return RTH_OPEN <= clock < RTH_CLOSE


def net_edge_bps(pre_fee_bps: Optional[float]) -> Optional[float]:
    """Pre-fee fillable edge minus 0.9 bps. Rebate is not added."""
    if pre_fee_bps is None:
        return None
    return float(pre_fee_bps) - ENTROPY_FEE_BPS - LIGHTER_FEE_BPS


def _percentile(values: list, p: float) -> float:
    xs = sorted(float(v) for v in values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p
    lo = int(math.floor(k))
    hi = int(math.ceil(k))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


def tail_vs_median(history: list, current: Optional[float]) -> Optional[str]:
    """Warn when the live net edge sits outside the central 80% of recent nets."""
    if current is None or len(history) < 8:
        return None
    med = _percentile(history, 0.5)
    lo = _percentile(history, 0.10)
    hi = _percentile(history, 0.90)
    if current < lo or current > hi:
        return (f"尾部相对中位数：当前净边际 {current:+.2f} bps，"
                f"中位数 {med:+.2f} bps")
    return None


def qualifying_direction(spec: dict, sell_pre: Optional[float],
                         buy_pre: Optional[float]) -> Optional[dict]:
    """Pick the side whose net edge clears the fee-aware band, if any.

    The engine adds taker fees on top of midline ± band, so a side qualifies
    when (pre-fee edge − 0.9) clears that hurdle. The larger net wins ties.
    """
    sell_net = net_edge_bps(sell_pre)
    buy_net = net_edge_bps(buy_pre)
    sell_hurdle = float(spec["midline_bps"]) + float(spec["upper_bps"])
    buy_hurdle = float(spec["lower_bps"]) - float(spec["midline_bps"])
    cands = []
    if sell_net is not None and sell_net >= sell_hurdle:
        cands.append(("sell_entropy", sell_pre, sell_net))
    if buy_net is not None and buy_net >= buy_hurdle:
        cands.append(("buy_entropy", buy_pre, buy_net))
    if not cands:
        return None
    cands.sort(key=lambda item: item[2], reverse=True)
    direction, pre, net = cands[0]
    return {
        "direction": direction,
        "pre_fee_edge_bps": round(float(pre), 4),
        "net_edge_bps": round(float(net), 4),
    }


def round_trip_fee_bps(measured_open_fee: Optional[float] = None) -> float:
    """Entropy open fee plus Entropy close fee. Lighter Standard stays ~0.

    The default is 1.8 bps. A measured Entropy taker fee replaces 0.9 on
    both legs of the round trip. Callers must not treat the one-way 0.9
    as if it already paid for the close.
    """
    one = ENTROPY_FEE_BPS
    if measured_open_fee is not None:
        try:
            candidate = float(measured_open_fee)
        except (TypeError, ValueError):
            candidate = None
        else:
            if math.isfinite(candidate) and candidate >= 0:
                one = candidate
    return ROUND_TRIP_OPEN_CLOSE * one + ROUND_TRIP_OPEN_CLOSE * LIGHTER_FEE_BPS


def round_trip_net_bps(pre_fee_edge_bps: Optional[float],
                       measured_open_fee: Optional[float] = None
                       ) -> Optional[float]:
    """Pre-fee edge minus the open+close fee. None when the edge is missing."""
    if pre_fee_edge_bps is None:
        return None
    try:
        pre = float(pre_fee_edge_bps)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(pre):
        return None
    return pre - round_trip_fee_bps(measured_open_fee)


def premium_inside_band(premium: Optional[float], spec: dict) -> bool:
    """True when premium has returned inside the task band, edges included.

    The close rule is the complement of the open band, not a midline touch.
    With midline -1.7 and ±1.0, inside means -2.7 <= premium <= -0.7.
    A print on either edge is already back; a print outside keeps the
    position. The caller still waits ``persist_sec`` before proposing.
    """
    if premium is None:
        return False
    try:
        px = float(premium)
        mid = float(spec["midline_bps"])
        upper = float(spec["upper_bps"])
        lower = float(spec["lower_bps"])
    except (TypeError, ValueError, KeyError):
        return False
    if not all(math.isfinite(v) for v in (px, mid, upper, lower)):
        return False
    return (mid - lower) <= px <= (mid + upper)


def _pos(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def symmetric_position(entropy_pos, lighter_pos) -> Optional[dict]:
    """Opposite, similar sizes. Returns the reduce-only close, or None.

    Entropy short / Lighter long closes with buy_entropy. The reverse
    closes with sell_entropy. Qty is the overlapping base size already on
    the books — nothing is invented from a model fill.
    """
    ent = _pos(entropy_pos)
    lig = _pos(lighter_pos)
    if ent is None or lig is None or ent == 0 or lig == 0:
        return None
    if ent * lig >= 0:
        return None
    gap = abs(abs(ent) - abs(lig))
    limit = max(NET_TOL_BASE, SYM_REL_TOL * max(abs(ent), abs(lig)))
    if gap > limit:
        return None
    if ent < 0 and lig > 0:
        direction, held = "buy_entropy", "sell_entropy"
    else:
        direction, held = "sell_entropy", "buy_entropy"
    return {
        "direction": direction,
        "held": held,
        "qty": min(abs(ent), abs(lig)),
    }


# Manual flatten is risk-off. It does not wait for the revert band and it
# does not take the open-order two-click force path outside RTH. A person
# still has to confirm. Fills are whatever the venue returns.
FLATTEN_NOTE = (
    "手动清仓随时可发，包括非 RTH、暂停和 HALT。不看回归带宽，"
    "也不走开仓的二次「强制确认」。仍要在弹窗里人工确认。"
    "成交数量只记交易所返回值，不编造。"
)


def _open_position(value) -> Optional[float]:
    """Signed size when it is past dust. None when flat, missing, or junk."""
    pos = _pos(value)
    if pos is None or abs(pos) <= NET_TOL_BASE:
        return None
    return pos


def _flatten_leg(venue: str, label: str, pos: float, qty: float) -> dict:
    closing_short = pos < 0
    return {
        "venue": venue,
        "label": label,
        "position": float(pos),
        "close_qty": float(qty),
        "is_buy": closing_short,
        "direction": "BUY" if closing_short else "SELL",
    }


def flatten_plan(entropy_pos, lighter_pos) -> Optional[dict]:
    """Reduce-only sizes that move SNDK toward flat. None when both are flat.

    A symmetric opposite book closes the overlapping base size on both
    legs (one ``execute_confirmed`` call with ``reduce_only``). Any other
    open book closes each venue's own absolute size toward zero. A missing
    read is not treated as zero and is not given an invented size.
    """
    ent = _pos(entropy_pos)
    lig = _pos(lighter_pos)
    ent_open = _open_position(ent)
    lig_open = _open_position(lig)
    if ent_open is None and lig_open is None:
        return None
    partial = ent is None or lig is None
    pair = None
    if ent_open is not None and lig_open is not None:
        pair = symmetric_position(ent_open, lig_open)
    if pair is not None:
        qty = float(pair["qty"])
        return {
            "kind": "pair",
            "direction": pair["direction"],
            "qty": qty,
            "reduce_only": True,
            "partial": False,
            "entropy_position": ent,
            "lighter_position": lig,
            "legs": [
                _flatten_leg("entropy", "Entropy", ent_open, qty),
                _flatten_leg("lighter", "Lighter", lig_open, qty),
            ],
        }
    legs = []
    if ent_open is not None:
        legs.append(_flatten_leg(
            "entropy", "Entropy", ent_open, abs(ent_open)))
    if lig_open is not None:
        legs.append(_flatten_leg(
            "lighter", "Lighter", lig_open, abs(lig_open)))
    if not legs:
        return None
    return {
        "kind": "legs",
        "direction": None,
        "qty": None,
        "reduce_only": True,
        "partial": partial,
        "entropy_position": ent,
        "lighter_position": lig,
        "legs": legs,
    }


def margin_headroom_notional(*, free_margin, leverage, safety: float
                             ) -> Optional[float]:
    """Notional headroom from free margin × leverage × safety.

    Returns None when either input is missing. Callers must refuse rather
    than substitute a cash notional. ``free_margin`` is spot USDC on
    Entropy (dex withdrawable can be 0 while isolated) and available
    balance on Lighter. A 10× isolated fill of about $11 locks about
    $1.11 of margin (notional / leverage, plus a small venue buffer).
    That locked number is not headroom; headroom is the cash still free.
    """
    margin = _pos(free_margin)
    lev = _pos(leverage)
    try:
        safe = float(safety)
    except (TypeError, ValueError):
        return None
    if margin is None or lev is None or not math.isfinite(safe):
        return None
    if lev <= 0 or margin < 0 or not 0 < safe <= 1:
        return None
    return margin * lev * safe


def size_order_notional(*, mode: str, hard_max_usd: float,
                        accounts: Optional[dict],
                        safety: float = MARGIN_SAFETY,
                        min_usd: float = 0.0) -> dict:
    """Cash mode returns the hard max. Margin mode sizes from both venues.

    The hard max always clips. Missing margin or leverage refuses; the
    cash hard max is not used as a stand-in.
    """
    try:
        hard = float(hard_max_usd)
    except (TypeError, ValueError):
        hard = None
    if hard is None or not math.isfinite(hard) or hard <= 0:
        return {"notional_usd": None, "source": mode, "refused": True,
                "reason": "hard max is not a positive number",
                "headroom_usd": None}
    if str(mode or "cash") != "margin":
        return {"notional_usd": hard, "source": "cash", "refused": False,
                "reason": None, "headroom_usd": None}
    accounts = accounts or {}
    rooms = []
    for name in ("entropy", "lighter"):
        leg = accounts.get(name) or {}
        room = margin_headroom_notional(
            free_margin=leg.get("available"),
            leverage=leg.get("leverage"),
            safety=safety,
        )
        if room is None:
            return {
                "notional_usd": None,
                "source": "margin",
                "refused": True,
                "reason": ("保证金或杠杆缺失，拒绝按保证金缩放"
                           "（不会改用现金名义）"),
                "headroom_usd": None,
            }
        rooms.append(room)
    headroom = min(rooms)
    notional = min(hard, headroom)
    if notional + 1e-9 < float(min_usd):
        return {
            "notional_usd": None,
            "source": "margin",
            "refused": True,
            "reason": (f"保证金×杠杆×{float(safety):.2f} 得到 "
                       f"${notional:.2f}，低于 ${float(min_usd):.0f}"),
            "headroom_usd": headroom,
        }
    return {"notional_usd": notional, "source": "margin", "refused": False,
            "reason": None, "headroom_usd": headroom}


def evaluate_auto_gates(*, intent: str, pre_fee_edge_bps: Optional[float],
                        entry_pre_fee_bps: Optional[float],
                        measured_fee_bps: Optional[float],
                        notional_usd: float, auto_confirm_max_usd: float,
                        fresh: bool, halted: bool, rth: bool,
                        funding_entropy: Optional[float],
                        funding_hedge: Optional[float],
                        available_entropy: Optional[float],
                        available_lighter: Optional[float],
                        available_need_entropy: float,
                        available_need_lighter: float,
                        daily_notional: float, daily_count: int,
                        daily_max_notional: Optional[float],
                        daily_max_count: Optional[float]) -> dict:
    """Gates that must pass before an auto open or an auto close.

    The round-trip check uses open+close fees (default 1.8 bps). An open
    is judged on the live pre-fee edge. A close is judged on the recorded
    entry edge, because the revert itself is inside the band and no longer
    shows an open signal. Outside RTH this never returns ok.
    """
    fee = round_trip_fee_bps(measured_fee_bps)
    basis = entry_pre_fee_bps if intent == "close" else pre_fee_edge_bps
    net = round_trip_net_bps(basis, measured_fee_bps)
    out = {
        "ok": False,
        "reason": None,
        "round_trip_fee_bps": fee,
        "round_trip_net_bps": None if net is None else round(float(net), 4),
    }

    def fail(reason: str) -> dict:
        out["reason"] = reason
        return out

    if halted:
        return fail("HALT")
    if not rth:
        return fail("非 RTH：自动确认不会强制发单")
    if not fresh:
        return fail("books not fresh")
    if net is None:
        return fail("没有可计算的往返边际")
    if net < 0:
        return fail(
            f"往返净边际 {net:.2f} bps 未覆盖开+平 {fee:.2f} bps"
            "（不用单边 0.9）")
    try:
        notional = float(notional_usd)
        auto_max = float(auto_confirm_max_usd)
    except (TypeError, ValueError):
        return fail("notional is not a number")
    if notional - auto_max > 1e-9:
        return fail("notional above auto_confirm_max_usd")
    if (daily_max_notional is not None
            and float(daily_notional) + notional - float(daily_max_notional) > 1e-9):
        return fail("daily notional cap")
    if (daily_max_count is not None
            and int(daily_count) + 1 > int(daily_max_count)):
        return fail("daily count cap")
    if _pos(funding_entropy) is None or _pos(funding_hedge) is None:
        return fail("funding not available")
    if intent != "close":
        ent_av = _pos(available_entropy)
        lig_av = _pos(available_lighter)
        if ent_av is None or lig_av is None:
            return fail("available not readable")
        if ent_av + 1e-9 < float(available_need_entropy):
            return fail("entropy available below margin")
        if lig_av + 1e-9 < float(available_need_lighter):
            return fail("lighter available below margin")
    out["ok"] = True
    return out


def trading_day(now: datetime) -> str:
    """America/New_York calendar day for the auto-confirm counter."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_new_york()).date().isoformat()


def leg_plan(direction: str, notional: float, accounts: Optional[dict],
             base_qty: Optional[float] = None) -> list:
    """Per-leg direction, notional, available, isolated. Secrets never appear."""
    accounts = accounts or {}
    ent = accounts.get("entropy") or {}
    lig = accounts.get("lighter") or {}
    if direction == "sell_entropy":
        ent_side, lig_side = "SELL", "BUY"
    else:
        ent_side, lig_side = "BUY", "SELL"
    legs = [
        {
            "venue": "Entropy",
            "dex": ENTROPY_DEX,
            "direction": ent_side,
            "notional_usd": float(notional),
            "available": ent.get("available"),
            "isolated": ent.get("isolated"),
        },
        {
            "venue": "Lighter",
            "direction": lig_side,
            "notional_usd": float(notional),
            "available": lig.get("available"),
            "isolated": lig.get("isolated"),
        },
    ]
    if base_qty is not None:
        for leg in legs:
            leg["base_qty"] = float(base_qty)
    return legs


def build_confirm_payload(spec: dict, proposal: dict, *, rth: bool,
                           tail: Optional[str]) -> dict:
    """The card the operator must accept. Accrual is a separate 未到账 line."""
    payload = {
        "proposal_id": proposal["proposal_id"],
        "direction": proposal["direction"],
        "net_edge_bps": proposal["net_edge_bps"],
        "pre_fee_edge_bps": proposal["pre_fee_edge_bps"],
        "fee_bps": ENTROPY_FEE_BPS,
        "accrual_bps": ACCRUAL_BPS,
        "accrual_label": ACCRUAL_LABEL,
        "referral_mode": REFERRAL_MODE,
        "midline_bps": float(spec["midline_bps"]),
        "rth": bool(rth),
        "rth_window": RTH_WINDOW,
        "legs": proposal["legs"],
        "tail_vs_median": tail,
        "confirm": True,
        "intent": proposal.get("intent") or "open",
        "reduce_only": bool(proposal.get("reduce_only")),
    }
    missing = missing_confirm_fields(payload)
    if missing:
        raise ValueError("confirm payload missing " + ", ".join(missing))
    return payload


def missing_confirm_fields(payload: object) -> list:
    """Names of required confirm-card fields that are absent or the wrong shape."""
    if not isinstance(payload, dict):
        return list(CONFIRM_FIELDS)
    missing = []
    for key in CONFIRM_FIELDS:
        if key not in payload:
            missing.append(key)
    legs = payload.get("legs")
    if "legs" not in missing:
        if not isinstance(legs, list) or len(legs) != 2:
            missing.append("legs")
        else:
            for idx, leg in enumerate(legs):
                if not isinstance(leg, dict):
                    missing.append(f"legs[{idx}]")
                    continue
                for field in LEG_FIELDS:
                    if field not in leg:
                        missing.append(f"legs[{idx}].{field}")
    return missing


def confirm_field_errors(payload: dict) -> list:
    """Semantic checks on a complete payload. Does not trust the client numbers."""
    errors = list(missing_confirm_fields(payload))
    if errors:
        return errors
    if payload.get("confirm") is not True:
        errors.append("confirm must be true")
    if payload.get("accrual_label") != ACCRUAL_LABEL:
        errors.append("accrual_label must be 未到账")
    if payload.get("referral_mode") != REFERRAL_MODE:
        errors.append("referral_mode must be self_t2")
    try:
        fee = float(payload["fee_bps"])
    except (TypeError, ValueError):
        errors.append("fee_bps must be 0.9")
    else:
        if abs(fee - ENTROPY_FEE_BPS) > 1e-9:
            errors.append("fee_bps must be 0.9")
    try:
        accrual = float(payload["accrual_bps"])
    except (TypeError, ValueError):
        errors.append("accrual_bps must be the self_t2 display accrual")
    else:
        if abs(accrual - ACCRUAL_BPS) > 1e-6:
            errors.append("accrual_bps must be the self_t2 display accrual")
    if not isinstance(payload.get("rth"), bool):
        errors.append("rth must be a boolean")
    tail = payload.get("tail_vs_median")
    if tail is not None and not isinstance(tail, str):
        errors.append("tail_vs_median must be a string or null")
    if not isinstance(payload.get("proposal_id"), str) or not payload["proposal_id"]:
        errors.append("proposal_id is required")
    if payload.get("intent") not in ("open", "close"):
        errors.append("intent must be open or close")
    return errors


def status_label(*, running: bool, paused: bool, mode: Optional[str],
                 live_armed: bool, fresh: bool) -> str:
    """Task-card pill. Live is never implied by a record-only process alone."""
    if paused and running:
        return STATUS_PAUSED
    if not running:
        return STATUS_STOPPED
    if mode == "live" and live_armed:
        return STATUS_LIVE if fresh else STATUS_WARMING
    return STATUS_RECORDING


def books_fresh(latest: Optional[dict], *, now: float,
                stale_sec: float = 180.0, min_samples: int = 15) -> bool:
    if not latest:
        return False
    ts = latest.get("minute_ts")
    samples = latest.get("samples")
    if not isinstance(ts, (int, float)) or not isinstance(samples, int):
        return False
    if samples < min_samples:
        return False
    return (now - float(ts)) <= stale_sec


def probe_config_dict(spec: dict) -> dict:
    """YAML body for ``.web/probe.yaml``. Strategy only — no credentials."""
    return {
        "thresholds": {
            "midline_bps": float(spec["midline_bps"]),
            "upper_bps": float(spec["upper_bps"]),
            "lower_bps": float(spec["lower_bps"]),
        },
        "entropy": {
            "dex": ENTROPY_DEX,
            "taker_fee_bps": ENTROPY_FEE_BPS,
            "max_position_usd": float(spec["max_position_usd"]),
            "max_orders_per_min": 120,
        },
        "hedge": {
            "taker_fee_bps": LIGHTER_FEE_BPS,
            "max_position_usd": float(spec["max_position_usd"]),
            "max_orders_per_min": 30,
        },
        "sizing": {
            "take_fraction": 0.2,
            "max_order_notional_usd": float(spec["order_notional_usd"]),
            "min_order_notional_usd": MIN_ORDER_USD,
        },
        "execution": {
            "premium_persist_sec": PERSIST_SEC,
        },
        "fees_ledger": {
            "referral_mode": REFERRAL_MODE,
            "rebate_accrual_only": True,
            "growth_haircut": 0.90,
        },
    }
