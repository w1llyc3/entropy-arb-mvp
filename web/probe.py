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
        "rth_only": True,
        "mode": "record",
    }


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


def leg_plan(direction: str, notional: float, accounts: Optional[dict]) -> list:
    """Per-leg direction, notional, available, isolated. Secrets never appear."""
    accounts = accounts or {}
    ent = accounts.get("entropy") or {}
    lig = accounts.get("lighter") or {}
    if direction == "sell_entropy":
        ent_side, lig_side = "SELL", "BUY"
    else:
        ent_side, lig_side = "BUY", "SELL"
    return [
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
