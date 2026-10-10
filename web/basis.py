"""Historical basis series for the localhost probe chart.

The chart reads recorded files only. It does not synthesize prices, premiums,
or fills.

``logs/minutes.csv`` (path from ``recorder.csv`` when that path stays inside
the repo) supplies the two price lines and the shaded basis. Required columns:

    minute_ts            unix seconds, minute start
    entropy_bid          Entropy top-of-book bid at the minute close (USD)
    entropy_ask          Entropy top-of-book ask at the minute close (USD)
    hedge_bid            hedge (Lighter) bid at the minute close (USD)
    hedge_ask            hedge ask at the minute close (USD)
    premium_close_bps    mid-to-mid premium of Entropy over the hedge, bps
                         (entropy_mid / hedge_mid - 1) * 10000

A price is the average of bid and ask when both are present and positive.
If only one side is positive, that side is the close. Non-positive quotes
are ignored. When ``premium_close_bps`` is blank, the basis is computed
from those same mids with the recorder's formula. A row that still has no
basis, or no price on either venue, is skipped.

Open/close markers need a timestamp that is already on disk:

    logs/trades.csv      ts, direction (sell_entropy or buy_entropy),
                         buy_fill, sell_fill
                         A row is a fill only when ts parses and buy_fill
                         or sell_fill is positive.

Open versus close is the running Entropy position implied by those fills,
starting from flat, because the CSV does not store a starting position.
``buy_entropy`` adds the Entropy-leg fill; ``sell_entropy`` subtracts it.
A fill that increases absolute position is an open. A fill that decreases
it is a close.

If ``logs/trades.csv`` has no such fills, one fallback is used: the panel's
``.web/risk.json`` ``last_order`` when it has ``ts`` and a positive fill,
and ``.web/probe.log`` names that ``confirm_id``'s direction. No other
timestamps are invented. When none of those are present, markers are
omitted.
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from web.probe import (
    DECISION_LOWER_BPS,
    DECISION_MIDLINE_BPS,
    DECISION_UPPER_BPS,
)
from web.report import recorder_csv_rel

# Hard cap so a long recorder file cannot turn the 45s poll into a large
# download. The newest rows are kept. Open/close is classified on the full
# fill list before this window is applied.
MAX_POINTS = 30000

REQUIRED_COLUMNS = (
    "minute_ts",
    "entropy_bid",
    "entropy_ask",
    "hedge_bid",
    "hedge_ask",
    "premium_close_bps",
)

MARKER_COLUMNS = ("ts", "direction", "buy_fill", "sell_fill")

DEFAULT_TRADES = "logs/trades.csv"

_ROUTE = re.compile(r"route confirm_id=(\S+) direction=(\S+)")
_DIRECTIONS = ("sell_entropy", "buy_entropy")


def trades_csv_rel(root: Path) -> str:
    """Relative trades path from config.yaml, else logs/trades.csv.

    Absolute paths and ``..`` are ignored, same rule as the minute CSV.
    """
    cfg = Path(root) / "config.yaml"
    if not cfg.is_file():
        return DEFAULT_TRADES
    try:
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return DEFAULT_TRADES
    logging_cfg = raw.get("logging") if isinstance(raw, dict) else None
    path = DEFAULT_TRADES
    if isinstance(logging_cfg, dict) and logging_cfg.get("trades_csv"):
        path = str(logging_cfg["trades_csv"]).strip() or DEFAULT_TRADES
    if not _rel_is_inside(path):
        return DEFAULT_TRADES
    return path


def _rel_is_inside(path: str) -> bool:
    if not path or os.path.isabs(path):
        return False
    return ".." not in Path(path).parts


def _inside(root: Path, rel: str) -> Optional[Path]:
    if not _rel_is_inside(rel):
        return None
    root_r = Path(root).resolve()
    path = (root_r / rel).resolve()
    try:
        path.relative_to(root_r)
    except ValueError:
        return None
    return path


def _blank(value) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float(value) -> Optional[float]:
    if isinstance(value, bool):
        return None
    text = _blank(value)
    if text is None:
        return None
    try:
        val = float(text)
    except (TypeError, ValueError):
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def _positive(value) -> Optional[float]:
    val = _float(value)
    if val is None or val <= 0:
        return None
    return val


def _mid(bid, ask) -> Optional[float]:
    """Close price from recorded bid/ask. None when neither side is usable."""
    b = _positive(bid)
    a = _positive(ask)
    if b is not None and a is not None:
        return (b + a) / 2.0
    return b if b is not None else a


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_bands(root: Path) -> dict:
    """Task midline and ± bands, else the Decision Card (-1.7, ±1)."""
    mid = float(DECISION_MIDLINE_BPS)
    upper = float(DECISION_UPPER_BPS)
    lower = float(DECISION_LOWER_BPS)
    used = False
    data = _read_json(Path(root) / ".web" / "task.json")
    if data:
        m = _float(data.get("midline_bps"))
        u = _float(data.get("upper_bps"))
        lo = _float(data.get("lower_bps"))
        if m is not None:
            mid = m
            used = True
        if u is not None and u > 0:
            upper = u
            used = True
        if lo is not None and lo > 0:
            lower = lo
            used = True
    return {
        "midline_bps": mid,
        "upper_bps": upper,
        "lower_bps": lower,
        "upper_line_bps": mid + upper,
        "lower_line_bps": mid - lower,
        "source": "task" if used else "default",
    }


def _columns_doc(minutes_rel: str, trades_rel: str) -> dict:
    return {
        "minutes_csv": minutes_rel,
        "required": list(REQUIRED_COLUMNS),
        "price": (
            "Minute close in USD. Mid is the average of bid and ask when "
            "both are present and positive; otherwise the one positive side."
        ),
        "basis": (
            "premium_close_bps, the recorded mid-to-mid premium of Entropy "
            "over the hedge. If that cell is blank, the same ratio is "
            "computed from the row's own bids and asks. No other basis is filled in."
        ),
        "markers_csv": trades_rel,
        "marker_columns": list(MARKER_COLUMNS),
        "markers": (
            "A marker needs ts and a positive buy_fill or sell_fill. "
            "direction must be sell_entropy or buy_entropy. Open/close follows "
            "the running Entropy position from those fills, starting flat. "
            "If the trades file has no fills, .web/risk.json last_order is "
            "used only when it has ts and a positive fill and .web/probe.log "
            "names that confirm_id's direction."
        ),
    }


def _empty_payload(root: Path, *, csv_rel: str, csv_exists: bool,
                   error: Optional[str], header_only: bool,
                   missing: list) -> dict:
    trades_rel = trades_csv_rel(root)
    note = error
    if note is None and not csv_exists:
        note = (
            f"{csv_rel} is not on disk yet. The chart stays empty until the "
            "recorder writes minute rows."
        )
    elif note is None and header_only:
        note = f"{csv_rel} has a header and no data rows."
    elif note is None:
        note = f"{csv_rel} has no usable minute rows."
    return {
        "csv_path": csv_rel,
        "csv_exists": csv_exists,
        "header_only": header_only,
        "points": [],
        "skipped_rows": 0,
        "truncated": False,
        "markers": [],
        "marker_source": None,
        "bands": load_bands(root),
        "columns": _columns_doc(csv_rel, trades_rel),
        "missing_columns": missing,
        "error": error,
        "note": note,
    }


def parse_minutes(path: Path) -> dict:
    """Parse a minutes CSV. An empty or missing file yields no points.

    Raises OSError or csv.Error to the caller. A header-only file and a
    zero-byte file both return an empty point list.
    """
    missing = list(REQUIRED_COLUMNS)
    result = {
        "points": [],
        "skipped_rows": 0,
        "header_only": False,
        "missing_columns": missing,
        "saw_row": False,
    }
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return result
        reader.fieldnames = [
            (name.strip() if isinstance(name, str) else name)
            for name in reader.fieldnames
        ]
        fields = set(reader.fieldnames)
        result["missing_columns"] = [c for c in REQUIRED_COLUMNS if c not in fields]
        saw = False
        by_ts: dict = {}
        for raw in reader:
            if not raw or not any(_blank(v) for v in raw.values()):
                continue
            saw = True
            point = _point_from_row(raw)
            if point is None:
                result["skipped_rows"] += 1
                continue
            by_ts[point["minute_ts"]] = point
        result["saw_row"] = saw
        result["header_only"] = not saw
        result["points"] = [by_ts[k] for k in sorted(by_ts)]
    return result


def _point_from_row(raw: dict) -> Optional[dict]:
    ts = _float(raw.get("minute_ts"))
    if ts is None:
        return None
    entropy = _mid(raw.get("entropy_bid"), raw.get("entropy_ask"))
    hedge = _mid(raw.get("hedge_bid"), raw.get("hedge_ask"))
    if entropy is None or hedge is None:
        return None
    basis = _float(raw.get("premium_close_bps"))
    source = "premium_close_bps"
    if basis is None and hedge != 0:
        basis = (entropy / hedge - 1.0) * 1e4
        source = "mids"
    if basis is None or math.isnan(basis) or math.isinf(basis):
        return None
    return {
        "minute_ts": ts,
        "time_utc": _blank(raw.get("time_utc")),
        "entropy_usd": entropy,
        "hedge_usd": hedge,
        "basis_bps": basis,
        "basis_source": source,
    }


def _fill_qty(raw: dict, direction: str) -> Optional[float]:
    buy = _positive(raw.get("buy_fill"))
    sell = _positive(raw.get("sell_fill"))
    if direction == "buy_entropy":
        qty = buy if buy is not None else sell
    elif direction == "sell_entropy":
        qty = sell if sell is not None else buy
    else:
        return None
    if qty is None or qty <= 0:
        return None
    return qty


def parse_trade_fills(path: Path) -> list:
    """Fills from logs/trades.csv. Rows without ts or a positive fill are dropped."""
    events = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return []
        reader.fieldnames = [
            (name.strip() if isinstance(name, str) else name)
            for name in reader.fieldnames
        ]
        for raw in reader:
            if not raw:
                continue
            ts = _float(raw.get("ts"))
            direction = (_blank(raw.get("direction")) or "")
            if ts is None or direction not in _DIRECTIONS:
                continue
            qty = _fill_qty(raw, direction)
            if qty is None:
                continue
            events.append({
                "ts": ts,
                "direction": direction,
                "qty": qty,
            })
    events.sort(key=lambda item: (item["ts"], item["direction"]))
    return events


def classify_fills(events: list) -> list:
    """Label each fill open or close from a flat starting position.

    ``events`` must already have ``ts``, ``direction``, and positive ``qty``.
    Order is by timestamp. The returned list keeps that order.
    """
    pos = 0.0
    out = []
    ordered = sorted(events, key=lambda item: (float(item["ts"]), item.get("direction") or ""))
    for ev in ordered:
        direction = ev.get("direction")
        qty = ev.get("qty")
        ts = ev.get("ts")
        if direction not in _DIRECTIONS or not isinstance(qty, (int, float)):
            continue
        if isinstance(qty, bool) or qty <= 0 or not isinstance(ts, (int, float)):
            continue
        if isinstance(ts, bool) or not math.isfinite(float(ts)):
            continue
        delta = float(qty) if direction == "buy_entropy" else -float(qty)
        before = pos
        pos = before + delta
        if abs(pos) > abs(before) + 1e-12:
            kind = "open"
        elif abs(pos) < abs(before) - 1e-12:
            kind = "close"
        else:
            continue
        out.append({
            "ts": float(ts),
            "kind": kind,
            "direction": direction,
            "qty": float(qty),
        })
    return out


def _panel_fill_event(root: Path) -> Optional[dict]:
    """One fill from the panel's last order, when the log names its direction."""
    risk = _read_json(Path(root) / ".web" / "risk.json") or {}
    last = risk.get("last_order")
    if not isinstance(last, dict):
        return None
    ts = _float(last.get("ts"))
    confirm_id = _blank(last.get("confirm_id"))
    if ts is None or not confirm_id:
        return None
    buy = _positive(last.get("buy_fill"))
    sell = _positive(last.get("sell_fill"))
    if buy is None and sell is None:
        return None
    log_path = Path(root) / ".web" / "probe.log"
    if not log_path.is_file():
        return None
    direction = None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        match = _ROUTE.search(line)
        if match and match.group(1) == confirm_id:
            direction = match.group(2)
    if direction not in _DIRECTIONS:
        return None
    if direction == "buy_entropy":
        qty = buy if buy is not None else sell
    else:
        qty = sell if sell is not None else buy
    if qty is None:
        return None
    return {"ts": ts, "direction": direction, "qty": qty}


def load_markers(root: Path) -> tuple:
    """Return ``(markers, source)``. Source is None when there is nothing to draw."""
    rel = trades_csv_rel(root)
    path = _inside(root, rel)
    events = []
    if path is not None and path.is_file():
        try:
            events = parse_trade_fills(path)
        except (OSError, csv.Error):
            events = []
    if events:
        return classify_fills(events), rel
    fallback = _panel_fill_event(root)
    if fallback:
        return classify_fills([fallback]), ".web/risk.json"
    return [], None


def _basis_note(points: list, markers: list, source: Optional[str],
                truncated: bool) -> str:
    if not points:
        return "No usable minute rows."
    sources = {p["basis_source"] for p in points}
    if sources == {"premium_close_bps"}:
        how = "premium_close_bps"
    elif sources == {"mids"}:
        how = "mids (premium_close_bps blank)"
    else:
        how = "premium_close_bps, with mids where that cell is blank"
    mark = "no open/close timestamps"
    if markers:
        mark = f"{len(markers)} open/close marker(s) from {source}"
    extra = " Newest 30000 minutes kept." if truncated else ""
    return f"{len(points)} minute(s), basis from {how}; {mark}.{extra}"


def build_basis(root: Path, hours: float = 0.0, now: Optional[float] = None,
                csv_rel: Optional[str] = None) -> dict:
    """Series, bands, and markers for the panel chart and the PNG tool."""
    root = Path(root)
    if csv_rel is None:
        rel = recorder_csv_rel(root)
    else:
        rel = csv_rel.strip()
        if not _rel_is_inside(rel):
            return _empty_payload(
                root, csv_rel=recorder_csv_rel(root), csv_exists=False,
                error="refusing a minutes path outside the repo",
                header_only=False, missing=list(REQUIRED_COLUMNS))
    path = _inside(root, rel)
    if path is None:
        return _empty_payload(
            root, csv_rel=recorder_csv_rel(root), csv_exists=False,
            error="refusing a minutes path outside the repo",
            header_only=False, missing=list(REQUIRED_COLUMNS))
    if not path.is_file():
        payload = _empty_payload(
            root, csv_rel=rel, csv_exists=False, error=None,
            header_only=False, missing=list(REQUIRED_COLUMNS))
        payload["markers"], payload["marker_source"] = load_markers(root)
        return payload
    try:
        parsed = parse_minutes(path)
    except (OSError, csv.Error) as exc:
        return _empty_payload(
            root, csv_rel=rel, csv_exists=True,
            error=f"could not read {rel}: {exc}",
            header_only=False, missing=list(REQUIRED_COLUMNS))

    clock = time_now() if now is None else float(now)
    if not math.isfinite(hours) or hours < 0:
        hours = 0.0
    if hours > 24 * 366:
        hours = 24 * 366
    cutoff = clock - hours * 3600.0 if hours > 0 else None

    points = parsed["points"]
    if cutoff is not None:
        points = [p for p in points if p["minute_ts"] >= cutoff]
    truncated = len(points) > MAX_POINTS
    if truncated:
        points = points[-MAX_POINTS:]

    markers, source = load_markers(root)
    if cutoff is not None:
        markers = [m for m in markers if m["ts"] >= cutoff]
    if points:
        start = points[0]["minute_ts"]
        end = points[-1]["minute_ts"]
        markers = [m for m in markers if start - 60.0 <= m["ts"] <= end + 60.0]

    if not points and cutoff is not None and parsed["points"]:
        note = f"No minute rows in the last {hours:g} hour(s)."
    elif not points and parsed["header_only"]:
        note = f"{rel} has a header and no data rows."
    elif not points and not parsed["saw_row"]:
        note = (
            f"{rel} is empty. Required columns: "
            + ", ".join(REQUIRED_COLUMNS) + "."
        )
    elif not points:
        note = (
            f"{rel} has no row with both venue closes and a basis. "
            "Required columns: " + ", ".join(REQUIRED_COLUMNS) + "."
        )
    else:
        note = _basis_note(points, markers, source, truncated)

    return {
        "csv_path": rel,
        "csv_exists": True,
        "header_only": bool(parsed["header_only"]),
        "points": points,
        "skipped_rows": parsed["skipped_rows"],
        "truncated": truncated,
        "markers": markers,
        "marker_source": source if markers else None,
        "bands": load_bands(root),
        "columns": _columns_doc(rel, trades_csv_rel(root)),
        "missing_columns": parsed["missing_columns"],
        "error": None,
        "note": note,
    }


def time_now() -> float:
    return datetime.now(timezone.utc).timestamp()
