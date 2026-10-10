#!/usr/bin/env python3
"""Write a static basis chart PNG from logs/minutes.csv.

The picture matches the panel: Entropy and Lighter minute closes on the
left axis (USD), premium_close_bps shaded green above 0 and red below 0
on the right axis, plus the task midline and ± bands. Open/close ticks
are drawn only when a fill timestamp is already on disk.

No extra packages. Run from the repo root:

    python3 tools/plot_basis.py
    python3 tools/plot_basis.py --hours 24 --out logs/basis.png

Default output is ``.web/basis.png``. Required minute columns are
minute_ts, entropy_bid, entropy_ask, hedge_bid, hedge_ask, and
premium_close_bps. See web/basis.py.
"""
from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web.basis import build_basis  # noqa: E402

# ASCII 32..126 of the Adafruit GFX classic 5x7 font (BSD). Column-major,
# bit 0 is the top pixel.
FONT = bytes((0,0,0,0,0,0,0,95,0,0,0,7,0,7,0,20,127,20,127,20,36,42,127,42,18,35,19,8,100,98,54,73,86,32,80,0,8,7,3,0,0,28,34,65,0,0,65,34,28,0,42,28,127,28,42,8,8,62,8,8,0,128,112,48,0,8,8,8,8,8,0,0,96,96,0,32,16,8,4,2,62,81,73,69,62,0,66,127,64,0,114,73,73,73,70,33,65,73,77,51,24,20,18,127,16,39,69,69,69,57,60,74,73,73,49,65,33,17,9,7,54,73,73,73,54,70,73,73,41,30,0,0,20,0,0,0,64,52,0,0,0,8,20,34,65,20,20,20,20,20,0,65,34,20,8,2,1,89,9,6,62,65,93,89,78,124,18,17,18,124,127,73,73,73,54,62,65,65,65,34,127,65,65,65,62,127,73,73,73,65,127,9,9,9,1,62,65,65,81,115,127,8,8,8,127,0,65,127,65,0,32,64,65,63,1,127,8,20,34,65,127,64,64,64,64,127,2,28,2,127,127,4,8,16,127,62,65,65,65,62,127,9,9,9,6,62,65,81,33,94,127,9,25,41,70,38,73,73,73,50,3,1,127,1,3,63,64,64,64,63,31,32,64,32,31,63,64,56,64,63,99,20,8,20,99,3,4,120,4,3,97,89,73,77,67,0,127,65,65,65,2,4,8,16,32,0,65,65,65,127,4,2,1,2,4,64,64,64,64,64,0,3,7,8,0,32,84,84,120,64,127,40,68,68,56,56,68,68,68,40,56,68,68,40,127,56,84,84,84,24,0,8,126,9,2,24,164,164,156,120,127,8,4,4,120,0,68,125,64,0,32,64,64,61,0,127,16,40,68,0,0,65,127,64,0,124,4,120,4,120,124,8,4,4,120,56,68,68,68,56,252,24,36,36,24,24,36,36,24,252,124,8,4,4,8,72,84,84,84,36,4,4,63,68,36,60,64,64,32,124,28,32,64,32,28,60,64,48,64,60,68,40,16,40,68,76,144,144,144,124,68,100,84,76,68,0,8,54,65,0,0,0,119,0,0,0,65,54,8,0,2,1,2,4,2))

W, H = 1200, 640
BG = (244, 241, 234)
INK = (31, 42, 51)
MUTED = (107, 114, 128)
GRID = (220, 214, 204)
ZERO = (168, 162, 152)
MID = (71, 85, 105)
BAND = (180, 148, 84)
ENTROPY = (47, 111, 224)
HEDGE = (31, 154, 74)
GREEN = (126, 196, 150)
RED = (224, 150, 150)
OPEN = (29, 78, 216)
CLOSE = (153, 52, 18)
_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class Canvas:
    def __init__(self, w: int, h: int, bg: tuple) -> None:
        self.w = w
        self.h = h
        self.bg = bg
        self.buf = bytearray(bytes(bg) * (w * h))

    def px(self, x: int, y: int, color: tuple) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.buf[i] = color[0]
            self.buf[i + 1] = color[1]
            self.buf[i + 2] = color[2]

    def blend(self, x: int, y: int, color: tuple, alpha: float) -> None:
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        i = (y * self.w + x) * 3
        for c in range(3):
            cur = self.buf[i + c]
            self.buf[i + c] = int(round(color[c] * alpha + cur * (1.0 - alpha)))

    def vline(self, x: int, y0: int, y1: int, color: tuple, alpha: float = 1.0) -> None:
        if y0 > y1:
            y0, y1 = y1, y0
        for y in range(y0, y1 + 1):
            if alpha >= 1:
                self.px(x, y, color)
            else:
                self.blend(x, y, color, alpha)

    def hline(self, x0: int, x1: int, y: int, color: tuple, dash: int = 0) -> None:
        if x0 > x1:
            x0, x1 = x1, x0
        for x in range(x0, x1 + 1):
            if dash and ((x // dash) % 2):
                continue
            self.px(x, y, color)

    def line(self, x0: int, y0: int, x1: int, y1: int, color: tuple) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.px(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def disk(self, x: int, y: int, r: int, color: tuple) -> None:
        r2 = r * r
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy <= r2:
                    self.px(x + dx, y + dy, color)

    def text(self, x: int, y: int, text: str, color: tuple, scale: int = 1) -> int:
        cursor = x
        for ch in text:
            o = ord(ch)
            if o < 32 or o > 126:
                cursor += 6 * scale
                continue
            start = (o - 32) * 5
            cols = FONT[start:start + 5]
            for cx, bits in enumerate(cols):
                for bit in range(8):
                    if bits & (1 << bit):
                        for sy in range(scale):
                            for sx in range(scale):
                                self.px(cursor + cx * scale + sx,
                                        y + bit * scale + sy, color)
            cursor += 6 * scale
        return cursor

    def png(self) -> bytes:
        raw = bytearray()
        stride = self.w * 3
        for y in range(self.h):
            raw.append(0)
            raw.extend(self.buf[y * stride:(y + 1) * stride])
        return _png(self.w, self.h, zlib.compress(bytes(raw), 9))


def _chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _png(w: int, h: int, compressed: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", compressed)
            + _chunk(b"IEND", b""))


def _nice_step(span: float, count: int) -> float:
    if span <= 0 or not math.isfinite(span):
        return 1.0
    raw = span / max(count, 1)
    if raw <= 0:
        return 1.0
    exp = math.floor(math.log10(raw))
    pow10 = 10.0 ** exp
    err = raw / pow10
    if err >= 7.5:
        factor = 10.0
    elif err >= 3.5:
        factor = 5.0
    elif err >= 1.5:
        factor = 2.0
    else:
        factor = 1.0
    return factor * pow10


def _ticks(lo: float, hi: float, count: int) -> list:
    step = _nice_step(hi - lo, count)
    if step <= 0:
        return []
    start = math.ceil(lo / step - 1e-9) * step
    out = []
    v = start
    guard = 0
    while v <= hi + step * 1e-6 and guard < 20:
        if lo - step * 0.2 <= v <= hi + step * 0.2:
            out.append(v)
        v += step
        guard += 1
    return out


def _fmt_px(v: float) -> str:
    return f"{v:,.2f}"


def _fmt_bps(v: float) -> str:
    if abs(v) >= 10:
        return f"{v:+.1f}"
    return f"{v:+.2f}"


def _fmt_time(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    hour = dt.hour
    ampm = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{_WD[dt.weekday()]} {h12}:{dt.minute:02d} {ampm}"


def _y(v: float, lo: float, hi: float, top: int, bottom: int) -> int:
    if hi == lo:
        return (top + bottom) // 2
    return top + int(round((hi - v) / (hi - lo) * (bottom - top)))


def render_png(payload: dict) -> bytes:
    """Raster of ``build_basis`` output. Empty input still returns a PNG."""
    canvas = Canvas(W, H, BG)
    points = list(payload.get("points") or [])
    left, right, top, bottom = 78, W - 74, 92, H - 46
    canvas.text(18, 14, "Price (USD) & Basis (bps)", INK, 2)
    canvas.text(18, 36, "Entropy mid vs Lighter mid  ·  premium close bps  ·  UTC",
                MUTED, 1)
    if not points:
        note = payload.get("note") or "No minute rows."
        canvas.text(left, top + 40, "No minute rows to plot.", INK, 2)
        # The 5x7 face is narrow; keep the required-column line short.
        canvas.text(left, top + 68, "Required columns:", MUTED, 1)
        canvas.text(left, top + 82, "minute_ts  entropy_bid  entropy_ask", MUTED, 1)
        canvas.text(left, top + 96, "hedge_bid  hedge_ask  premium_close_bps", MUTED, 1)
        short = note.replace("\n", " ")
        if len(short) > 90:
            short = short[:87] + "..."
        canvas.text(left, top + 118, short, MUTED, 1)
        return canvas.png()

    bands = payload.get("bands") or {}
    mid = float(bands.get("midline_bps", -1.7))
    up_line = float(bands.get("upper_line_bps", mid + float(bands.get("upper_bps", 1))))
    lo_line = float(bands.get("lower_line_bps", mid - float(bands.get("lower_bps", 1))))
    prices = [p["entropy_usd"] for p in points] + [p["hedge_usd"] for p in points]
    p_lo, p_hi = min(prices), max(prices)
    pad_p = (p_hi - p_lo) * 0.08 or max(abs(p_hi) * 0.002, 0.01)
    p_lo -= pad_p
    p_hi += pad_p
    basis_vals = [p["basis_bps"] for p in points] + [0.0, mid, up_line, lo_line]
    b_lo, b_hi = min(basis_vals), max(basis_vals)
    pad_b = (b_hi - b_lo) * 0.12 or 1.0
    b_lo -= pad_b
    b_hi += pad_b

    t0 = points[0]["minute_ts"]
    t1 = points[-1]["minute_ts"]
    if t1 <= t0:
        t0 -= 60.0
        t1 += 60.0
    else:
        span = (t1 - t0) * 0.02
        t0 -= span
        t1 += span

    def x_of(ts: float) -> int:
        return left + int(round((ts - t0) / (t1 - t0) * (right - left)))

    def y_px(v: float) -> int:
        return _y(v, p_lo, p_hi, top, bottom)

    def y_bps(v: float) -> int:
        return _y(v, b_lo, b_hi, top, bottom)

    # Shaded basis vs 0, behind the price lines.
    cols = {}
    for p in points:
        cols[x_of(p["minute_ts"])] = p["basis_bps"]
    xs = sorted(cols)
    if len(xs) == 1:
        v = cols[xs[0]]
        color = GREEN if v >= 0 else RED
        ya = min(max(y_bps(v), top), bottom)
        yb = min(max(y_bps(0), top), bottom)
        for dx in (-1, 0, 1):
            x = xs[0] + dx
            if left <= x <= right:
                canvas.vline(x, ya, yb, color, 0.55)
    for i in range(len(xs) - 1):
        x0, x1 = xs[i], xs[i + 1]
        v0, v1 = cols[x0], cols[x1]
        span = x1 - x0
        if span <= 0:
            continue
        for x in range(max(x0, left), min(x1, right) + 1):
            v = v0 + (v1 - v0) * ((x - x0) / span)
            color = GREEN if v >= 0 else RED
            ya = min(max(y_bps(v), top), bottom)
            yb = min(max(y_bps(0), top), bottom)
            canvas.vline(x, ya, yb, color, 0.5)

    for v in _ticks(b_lo, b_hi, 5):
        y = y_bps(v)
        if top <= y <= bottom:
            canvas.hline(left, right, y, GRID, 0)

    canvas.hline(left, right, y_bps(0), ZERO, 4)
    canvas.hline(left, right, y_bps(mid), MID, 5)
    canvas.hline(left, right, y_bps(up_line), BAND, 3)
    canvas.hline(left, right, y_bps(lo_line), BAND, 3)

    for marker in payload.get("markers") or []:
        ts = marker.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        if ts < t0 or ts > t1:
            continue
        x = x_of(float(ts))
        color = OPEN if marker.get("kind") == "open" else CLOSE
        for y in range(top, bottom + 1, 3):
            canvas.px(x, y, color)
        label = "open" if marker.get("kind") == "open" else "close"
        canvas.text(x + 3, top + 2, label, color, 1)

    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        x0, x1 = x_of(a["minute_ts"]), x_of(b["minute_ts"])
        canvas.line(x0, y_px(a["entropy_usd"]), x1, y_px(b["entropy_usd"]), ENTROPY)
        canvas.line(x0, y_px(a["entropy_usd"]) + 1, x1, y_px(b["entropy_usd"]) + 1, ENTROPY)
        canvas.line(x0, y_px(a["hedge_usd"]), x1, y_px(b["hedge_usd"]), HEDGE)
        canvas.line(x0, y_px(a["hedge_usd"]) + 1, x1, y_px(b["hedge_usd"]) + 1, HEDGE)
    if len(points) == 1:
        x = x_of(points[0]["minute_ts"])
        canvas.disk(x, y_px(points[0]["entropy_usd"]), 3, ENTROPY)
        canvas.disk(x, y_px(points[0]["hedge_usd"]), 3, HEDGE)

    # Axes and ticks.
    canvas.hline(left, right, bottom, INK, 0)
    canvas.vline(left, top, bottom, INK, 1)
    canvas.vline(right, top, bottom, INK, 1)
    for v in _ticks(p_lo, p_hi, 4):
        y = y_px(v)
        if y < top or y > bottom:
            continue
        label = _fmt_px(v)
        canvas.text(8, y - 4, label, MUTED, 1)
    for v in _ticks(b_lo, b_hi, 5):
        y = y_bps(v)
        if y < top or y > bottom:
            continue
        canvas.text(right + 8, y - 4, _fmt_bps(v), MUTED, 1)
    canvas.text(right + 8, top - 14, "bps", MUTED, 1)
    canvas.text(8, top - 14, "USD", MUTED, 1)

    # A few time labels along the bottom.
    n_lab = 4 if (points[-1]["minute_ts"] - points[0]["minute_ts"]) > 0 else 1
    last_x = -10_000
    for i in range(n_lab):
        if n_lab == 1:
            ts = points[0]["minute_ts"]
        else:
            ts = points[0]["minute_ts"] + (
                points[-1]["minute_ts"] - points[0]["minute_ts"]) * i / (n_lab - 1)
        x = x_of(ts)
        label = _fmt_time(ts)
        width = len(label) * 6
        if x - width // 2 < last_x + 8:
            continue
        canvas.text(max(left, x - width // 2), bottom + 10, label, MUTED, 1)
        last_x = x + width // 2

    # Legend under the subtitle.
    canvas.disk(22, 64, 4, ENTROPY)
    canvas.text(30, 58, "Entropy", ENTROPY, 1)
    canvas.disk(110, 64, 4, HEDGE)
    canvas.text(118, 58, "Lighter", HEDGE, 1)
    canvas.disk(200, 64, 4, GREEN)
    canvas.text(208, 58, "basis > 0", (46, 120, 78), 1)
    canvas.disk(300, 64, 4, RED)
    canvas.text(308, 58, "basis < 0", (160, 64, 64), 1)
    canvas.text(400, 58, "mid " + _fmt_bps(mid), MID, 1)
    canvas.text(490, 58, "bands " + _fmt_bps(lo_line) + " " + _fmt_bps(up_line), BAND, 1)
    return canvas.png()


def write_png(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_png(payload))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Write a basis chart PNG from logs/minutes.csv")
    parser.add_argument("--root", default=".",
                        help="repo root (default: current directory)")
    parser.add_argument("--csv", default="",
                        help="minutes CSV relative to --root "
                             "(default: recorder.csv or logs/minutes.csv)")
    parser.add_argument("--hours", type=float, default=0.0,
                        help="only the last N hours (0 = all rows)")
    parser.add_argument("--out", default=".web/basis.png",
                        help="PNG path (default: .web/basis.png)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    payload = build_basis(root, hours=args.hours, csv_rel=args.csv or None)
    if payload.get("error") and str(payload["error"]).startswith("refusing"):
        print(payload["error"], file=sys.stderr)
        sys.exit(2)
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    write_png(out, payload)
    print(f"wrote {out} ({len(payload.get('points') or [])} minutes, "
          f"{len(payload.get('markers') or [])} markers)")
    if payload.get("note"):
        print(payload["note"])


if __name__ == "__main__":
    main()
