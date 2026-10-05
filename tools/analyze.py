#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the CSV written by the built-in recorder (logs/minutes.csv by default)
and prints:

  * the premium distribution (midline candidates),
  * fillable-at-$100 edge: pre-fee p50, the Gate (net of fees, rebate 0),
    and an accrual-rebate figure that is display-only,
  * slip@100 p90 and mean depth_ok_frac (G3 / G4),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

When the CSV has ``fill_*_edge_100_bps`` columns, firing stats use those
minute means. Older files fall back to top-of-book ``sell/buy_edge_max``
with a warning.

分析机器人自动采集的分钟级盘口数据。Gate 用 $100 可成交边际溢价减去手续费
（返佣强制为 0）。应计返佣只展示，不参与 Gate。

Usage:
    python3 tools/analyze.py
    python3 tools/analyze.py --hours 24 --fees-bps 0.9 --min-samples 48
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from entropy_arb.config import (  # noqa: E402
    ENTROPY_FEE_SHARE,
    REFERRAL_RATES,
    recognized_rebate_bps,
)

CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]

FILL_SELL = "fill_sell_edge_100_bps"
FILL_BUY = "fill_buy_edge_100_bps"
SLIP_SELL = "slip_sell_100_bps"
SLIP_BUY = "slip_buy_100_bps"
DEPTH = "depth_ok_frac"


def pctl(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _opt_float(raw) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        val = float(text)
    except ValueError:
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def load_rebate_assumptions(config_path: str) -> dict:
    """Display-only rebate inputs. Missing/unreadable config uses shipped defaults.

    The Gate never calls this. Recognized rebate applies ``growth_haircut``
    to Entropy's fee share, then the configured referral rate (shipped
    mode is referred_t4 = 100% of that share).
    """
    entropy_fee = 0.9
    mode = "referred_t4"
    haircut = 0.90
    source = "built-in SNDK+lighter defaults"
    if config_path and os.path.exists(config_path):
        try:
            import yaml
            with open(config_path) as fh:
                raw = yaml.safe_load(fh) or {}
            ent = raw.get("entropy") or {}
            if isinstance(ent, dict) and "taker_fee_bps" in ent:
                entropy_fee = float(ent["taker_fee_bps"])
            led = raw.get("fees_ledger") or {}
            if isinstance(led, dict):
                if "referral_mode" in led:
                    mode = str(led["referral_mode"])
                if "growth_haircut" in led:
                    haircut = float(led["growth_haircut"])
            source = config_path
        except Exception as exc:
            print(f"warning: could not read {config_path} ({exc}); "
                  f"accrual display uses built-in defaults", file=sys.stderr)
    try:
        rebate = recognized_rebate_bps(entropy_fee, haircut, mode)
    except Exception as exc:
        print(f"warning: rebate assumptions rejected ({exc}); "
              f"accrual display uses referred_t4 / haircut 0.90 / fee 0.9",
              file=sys.stderr)
        entropy_fee, mode, haircut = 0.9, "referred_t4", 0.90
        rebate = recognized_rebate_bps(entropy_fee, haircut, mode)
        source = "built-in SNDK+lighter defaults"
    return {
        "entropy_fee_bps": entropy_fee,
        "referral_mode": mode,
        "referral_rate": REFERRAL_RATES.get(mode, REFERRAL_RATES["referred_t4"]),
        "growth_haircut": haircut,
        "rebate_bps": rebate,
        "source": source,
    }


def load_rows(path: str, hours: float, min_samples: int) -> tuple:
    """Return ``(rows, used_fillable)``.

    ``used_fillable`` is true when the CSV header has the $100 fillable
    columns. Blank cells (depth too thin) stay None and do not fire.
    """
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    rows = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        used_fillable = FILL_SELL in fields and FILL_BUY in fields
        has_slip = SLIP_SELL in fields and SLIP_BUY in fields
        has_depth = DEPTH in fields
        for r in reader:
            try:
                if float(r["minute_ts"]) < cutoff:
                    continue
                if int(float(r["samples"])) < min_samples:
                    continue
                row = {
                    "ts": float(r["minute_ts"]),
                    "prem": float(r["premium_close_bps"]),
                    "prem_mean": float(r["premium_mean_bps"]),
                    "sell_max": float(r["sell_edge_max_bps"]),
                    "buy_max": float(r["buy_edge_max_bps"]),
                    "sell_fill": _opt_float(r.get(FILL_SELL)) if used_fillable else None,
                    "buy_fill": _opt_float(r.get(FILL_BUY)) if used_fillable else None,
                    "slip_sell": _opt_float(r.get(SLIP_SELL)) if has_slip else None,
                    "slip_buy": _opt_float(r.get(SLIP_BUY)) if has_slip else None,
                    "depth_ok": _opt_float(r.get(DEPTH)) if has_depth else None,
                }
                rows.append(row)
            except (KeyError, ValueError, TypeError):
                continue
    return rows, used_fillable


def _edge_for(row: dict, side: str, used_fillable: bool) -> float | None:
    if used_fillable:
        return row["sell_fill"] if side == "sell" else row["buy_fill"]
    return row["sell_max"] if side == "sell" else row["buy_max"]


def firing_room(rows: list, side: str, midline: float, fees: float,
                used_fillable: bool) -> list:
    """Fee-adjusted room beyond the midline. Missing fillable samples do not fire."""
    out = []
    for r in rows:
        edge = _edge_for(r, side, used_fillable)
        if edge is None:
            out.append(float("-inf"))
            continue
        if side == "sell":
            out.append(edge - midline - fees)
        else:
            out.append(edge + midline - fees)
    return out


def summarize_fillable(rows: list, fees: float, rebate_bps: float,
                       used_fillable: bool) -> dict:
    """p50 pre-fee / gate / accrual, plus G3 slip p90 and G4 depth."""
    out = {"used_fillable": used_fillable, "fees": fees, "rebate_bps": rebate_bps}
    for side, key in (("sell", "sell"), ("buy", "buy")):
        vals = [v for v in (_edge_for(r, side, used_fillable) for r in rows)
                if v is not None]
        vals.sort()
        pre = pctl(vals, 50)
        out[side] = {
            "n": len(vals),
            "pre_p50": pre,
            "gate_p50": pre - fees,
            "accrual_p50": pre - fees + rebate_bps,
        }
    slips = {}
    for side, col in (("sell", "slip_sell"), ("buy", "slip_buy")):
        vals = sorted(r[col] for r in rows if r[col] is not None)
        slips[side] = {"n": len(vals), "p90": pctl(vals, 90)}
    out["slip"] = slips
    depths = [r["depth_ok"] for r in rows if r["depth_ok"] is not None]
    out["depth_n"] = len(depths)
    out["depth_mean"] = (sum(depths) / len(depths)) if depths else float("nan")
    return out


def _fmt(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    return f"{val:+.2f}"


def print_fillable_summary(summary: dict, assumptions: dict) -> None:
    basis = ("fillable@$100 minute-mean" if summary["used_fillable"]
             else "FALLBACK top-of-book edge max")
    fees = summary["fees"]
    rebate = summary["rebate_bps"]
    print(f"fillable edge @ $100 notional — median (p50), basis: {basis}")
    print(f"  {'direction':<16} | {'pre-fee p50':>12} | "
          f"{'GATE net p50':>12} | {'accrual p50':>12}")
    print(f"  {'':<16} | {'':>12} | {'rebate=0':>12} | {'display only':>12}")
    for side, label in (("sell", "SELL entropy"), ("buy", "BUY entropy")):
        s = summary[side]
        print(f"  {label:<16} | {_fmt(s['pre_p50']):>12} | "
              f"{_fmt(s['gate_p50']):>12} | {_fmt(s['accrual_p50']):>12}"
              f"   (n={s['n']})")
    print(f"  GATE = pre-fee p50 − {fees:.1f} bps taker fees. "
          f"Rebate is forced to 0. This is the only gate number.")
    rate_pct = assumptions["referral_rate"] * 100.0
    kept = (1.0 - assumptions["growth_haircut"]) * 100.0
    print(f"  Accrual (NOT a gate, never realized cash), from "
          f"{assumptions['source']}:")
    print(f"    Entropy fee {assumptions['entropy_fee_bps']:.2f} bps "
          f"× share {ENTROPY_FEE_SHARE:.2f} "
          f"× keep {kept:.0f}% after growth_haircut "
          f"{assumptions['growth_haircut']:.2f} "
          f"× {assumptions['referral_mode']} {rate_pct:.0f}% "
          f"= {rebate:.4f} bps recognized.")
    print(f"    accrual p50 = GATE p50 + {rebate:.4f}. "
          f"Do not trade off this column.")
    ss, sb = summary["slip"]["sell"], summary["slip"]["buy"]
    print(f"  G3 slip@$100 p90 (TOB edge − average-fill edge, bps): "
          f"SELL {_fmt(ss['p90'])} (n={ss['n']})   "
          f"BUY {_fmt(sb['p90'])} (n={sb['n']})")
    if summary["depth_n"]:
        print(f"  G4 mean depth_ok_frac "
              f"(both directions fillable at ≥$100): "
              f"{summary['depth_mean']:.4f} over {summary['depth_n']} minutes")
    else:
        print("  G4 mean depth_ok_frac: n/a (column absent or blank)")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--config", default="config.yaml",
                   help="read fees_ledger / entropy fee for the display-only "
                        "accrual column (default: config.yaml)")
    p.add_argument("--hours", type=float, default=0.0,
                   help="only use the last N hours (0 = all data)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples than this")
    p.add_argument("--fees-bps", type=float, default=0.9,
                   help="SUM of both venues' taker fees in bps (each crossing "
                        "pays both legs). Recorded edges are pre-fee. Default "
                        "0.9 = Entropy 0.9 + Lighter 0 for SNDK --hedge lighter. "
                        "The Gate subtracts this with rebate forced to 0. "
                        "Pass a higher sum for a tradexyz hedge.")
    args = p.parse_args()

    try:
        rows, used_fillable = load_rows(args.csv, args.hours, args.min_samples)
    except FileNotFoundError:
        print(f"{args.csv} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    if not used_fillable and rows:
        print("warning: fill_sell_edge_100_bps / fill_buy_edge_100_bps missing "
              "— falling back to tob sell_edge_max_bps / buy_edge_max_bps. "
              "Gate stats are top-of-book, not fillable@$100.",
              file=sys.stderr)
    if len(rows) < 30:
        print(f"only {len(rows)} usable minute(s) in {args.csv} — collect at "
              f"least a few hours before trusting the numbers / 数据太少，"
              f"建议至少采集数小时", file=sys.stderr)
        if not rows:
            sys.exit(1)

    assumptions = load_rebate_assumptions(args.config)
    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)

    print(f"\n=== {args.csv}: {len(rows)} minutes over {span_h:.1f}h ===\n")
    print("premium of Entropy over hedge, minute close (bps) / "
          "Entropy 相对对冲腿的溢价:")
    print(f"  mean {mean:+.2f}   std {math.sqrt(var):.2f}   "
          f"median (p50) {median:+.2f}")
    print(f"  p5 {pctl(prem, 5):+.2f}   p25 {pctl(prem, 25):+.2f}   "
          f"p75 {pctl(prem, 75):+.2f}   p95 {pctl(prem, 95):+.2f}")
    print()
    summary = summarize_fillable(rows, args.fees_bps, assumptions["rebate_bps"],
                                 used_fillable)
    print_fillable_summary(summary, assumptions)

    midline = round(median, 1) or 0.0   # normalize -0.0
    # room beyond the midline that was actually executable each minute, net
    # of taker fees (config thresholds are net-of-fee: the engine adds fees
    # on top, and recorded edges are pre-fee). Rebate stays 0 here — the
    # accrual figure above is not an input.
    fees = args.fees_bps
    sell_room = firing_room(rows, "sell", midline, fees, used_fillable)
    buy_room = firing_room(rows, "buy", midline, fees, used_fillable)
    basis = ("fillable@$100" if used_fillable
             else "top-of-book max (fillable columns missing)")

    print(f"with midline_bps = {midline:+.1f} (median / p50 of premium close) "
          f"and {fees:.1f} bps one-crossing taker fees (rebate 0), minutes "
          f"each band would have fired on {basis} / 各档净阈值触发的分钟数:")
    print(f"  {'band bps':>9} | {'SELL entropy':>17} | {'BUY entropy':>17}")
    print(f"  {'':>9} | {'minutes':>8} {'per day':>8} | "
          f"{'minutes':>8} {'per day':>8}")
    per_day = 24.0 / span_h if span_h > 0 else 0.0
    for t in CANDIDATES:
        s_hits = sum(1 for x in sell_room if x >= t)
        b_hits = sum(1 for x in buy_room if x >= t)
        print(f"  {t:>9.1f} | {s_hits:>8} {s_hits * per_day:>8.1f} | "
              f"{b_hits:>8} {b_hits * per_day:>8.1f}")

    # default suggestion: the band that fired in ~10% of minutes (p90 of the
    # fee-adjusted executable room), floored at 1 bps — tune from the table.
    # -inf from unfilled minutes sorts to the left and does not lift p90 of
    # the right tail the way a missing observation would if dropped; p90 of
    # the room including non-fills is the conservative "fired ~10%" cut.
    sug_upper = max(round(pctl(sorted(sell_room), 90) * 2) / 2, 1.0)
    sug_lower = max(round(pctl(sorted(buy_room), 90) * 2) / 2, 1.0)
    if math.isnan(sug_upper):
        sug_upper = 1.0
    if math.isnan(sug_lower):
        sug_lower = 1.0
    print(f"""
suggested starting point (fires ~10% of minutes on {basis}, already net of the
{fees:.1f} bps fees passed via --fees-bps with rebate forced to 0; a full
round trip nets >= upper+lower bps after fees) /
建议起点（约 10% 的分钟触发；已扣除 --fees-bps 传入的 {fees:.1f} bps 手续费，
返佣按 0 计，一次完整往返扣费后净赚 >= upper+lower bps）:

thresholds:
  midline_bps: {midline}
  upper_bps: {sug_upper}
  lower_bps: {sug_lower}

Re-run with --hours to focus on recent regimes; premiums drift, so refresh
these numbers regularly. / 溢价中枢会漂移，请定期重新分析并更新配置。
The accrual-rebate column above is not an input to these thresholds.
""")


if __name__ == "__main__":
    main()
