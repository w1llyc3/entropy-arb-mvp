#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the CSV written by the built-in recorder (logs/minutes.csv by default)
and prints:

  * the premium distribution (midline candidates),
  * fillable-at-$100 contrast columns: pre-fee p50, net p50 (rebate 0),
    and an accrual-rebate figure that is display-only,
  * Gates G1–G4 (worse net median, ±50% band robustness, slip vs net edge,
    thin_frac < 0.30 sizing signal),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

When the CSV has ``fill_*_edge_100_bps`` columns, firing stats use those
minute means. Older files fall back to top-of-book ``sell/buy_edge_max``
with a warning.

分析机器人自动采集的分钟级盘口数据。G1 取买卖两侧 $100 可成交净边际
（手续费按 --fees-bps，返佣强制为 0）的较差中位数。应计返佣只展示，不参与 Gate。

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

# G4 is a shallow-book sizing signal. PASS iff thin_frac is strictly below
# this cut. It does not stop the recorder and it does not place orders.
THIN_FRAC_MAX = 0.30

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
    """Per-side p50 pre-fee / net / accrual, plus slip p90 and depth.

    ``net_p50`` is pre-fee median minus ``fees`` with rebate forced to 0.
    It is a contrast column. Gate G1 is the worse of the two sides, computed
    later by :func:`evaluate_gates`.
    """
    out = {"used_fillable": used_fillable, "fees": fees, "rebate_bps": rebate_bps}
    for side in ("sell", "buy"):
        vals = [v for v in (_edge_for(r, side, used_fillable) for r in rows)
                if v is not None]
        vals.sort()
        pre = pctl(vals, 50)
        out[side] = {
            "n": len(vals),
            "pre_p50": pre,
            "net_p50": pre - fees,
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
    # A minute is entirely too thin when no sample had both sides fillable
    # at >= $100 (depth_ok_frac == 0).
    thin = sum(1 for d in depths if d == 0.0)
    out["depth_thin_n"] = thin
    out["depth_thin_frac"] = (thin / len(depths)) if depths else float("nan")
    return out


def _fmt(val: float) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return "n/a"
    if isinstance(val, float) and math.isinf(val):
        return "-inf" if val < 0 else "+inf"
    return f"{val:+.2f}"


def _finite(val) -> bool:
    return isinstance(val, (int, float)) and math.isfinite(val)


def _verdict(flag: bool) -> str:
    return "PASS" if flag else "FAIL"


def _basis_label(used_fillable: bool) -> str:
    return ("fillable@$100 minute-mean" if used_fillable
            else "FALLBACK top-of-book edge max")


def print_fillable_summary(summary: dict, assumptions: dict) -> None:
    basis = _basis_label(summary["used_fillable"])
    fees = summary["fees"]
    rebate = summary["rebate_bps"]
    print(f"fillable edge @ $100 notional — median (p50), basis: {basis}")
    print(f"  {'direction':<16} | {'pre-fee p50':>12} | "
          f"{'net p50':>12} | {'accrual p50':>12}")
    print(f"  {'':<16} | {'':>12} | {'rebate=0':>12} | {'display only':>12}")
    for side, label in (("sell", "SELL entropy"), ("buy", "BUY entropy")):
        s = summary[side]
        print(f"  {label:<16} | {_fmt(s['pre_p50']):>12} | "
              f"{_fmt(s['net_p50']):>12} | {_fmt(s['accrual_p50']):>12}"
              f"   (n={s['n']})")
    print(f"  net p50 = pre-fee p50 − {fees:.1f} bps taker fees, rebate 0. "
          f"Contrast column only — not a Gate id. G1 is the worse of the two.")
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
    print(f"    accrual p50 = net p50 + {rebate:.4f}. "
          f"Do not trade off this column.")
    print()


def _g1(summary: dict) -> dict:
    """Worse of the two fillable@$100 net medians. Pass iff > 0."""
    sell = summary["sell"]["net_p50"]
    buy = summary["buy"]["net_p50"]
    label = ("fillable@$100 conservative net-fee median > 0 "
             "(worse of SELL/BUY, rebate 0)")
    if not _finite(sell) or not _finite(buy):
        return {"value": None, "pass": False, "label": label,
                "sell": sell, "buy": buy}
    value = min(sell, buy)
    return {"value": value, "pass": value > 0, "label": label,
            "sell": sell, "buy": buy}


def _shifted_firings(room: list, base: float):
    """×0.5 and ×1.5 hurdles, and the net edge (room) of each firing."""
    if not _finite(base) and not (isinstance(base, float) and math.isinf(base)):
        return None
    shifts = (base * 0.5, base * 1.5)
    fired = [edge for hurdle in shifts for edge in room if edge >= hurdle]
    return shifts, fired


def _g2(sell_room: list, buy_room: list) -> dict:
    """±50% shift of the p90 upper/lower bands.

    Bases are the p90 of fee-adjusted room (rebate already 0), before the
    1 bps floor used on the pasted suggestion. A minute fires when its room
    is at least the shifted hurdle. That firing's net edge is the room.
    Pass iff every firing is >= 0. No firings is a pass (nothing negative).
    The number is the worst firing net edge.
    """
    label = ("±50% shift of p90 upper/lower (before 1 bps floor); "
             "worst firing net edge >= 0")
    sides = {}
    fired: list = []
    undefined = False
    for name, room in (("upper", sell_room), ("lower", buy_room)):
        base = pctl(sorted(room), 90)
        shifted = _shifted_firings(room, base)
        if shifted is None:
            undefined = True
            sides[name] = {"p90": base, "shifts": None}
            continue
        shifts, edges = shifted
        sides[name] = {"p90": base, "shifts": shifts}
        fired.extend(edges)
    if undefined:
        return {"value": None, "pass": False, "label": label,
                "fired": False, "sides": sides}
    finite = [e for e in fired if _finite(e)]
    if len(finite) != len(fired):
        # A non-finite room fired (hurdle was infinite). That edge is not >= 0.
        return {"value": None, "pass": False, "label": label,
                "fired": True, "sides": sides}
    if not finite:
        return {"value": None, "pass": True, "label": label,
                "fired": False, "sides": sides}
    worst = min(finite)
    return {"value": worst, "pass": worst >= 0, "label": label,
            "fired": True, "sides": sides}


def _side_slip_check(net, slip) -> dict:
    if not _finite(net) or not _finite(slip):
        return {"pass": False, "slack": None, "net": net, "slip": slip}
    slack = net - slip
    return {"pass": slip < net, "slack": slack, "net": net, "slip": slip}


def _g3(summary: dict) -> dict:
    """Each side: slip@$100 p90 < that side's net p50. Number is worse slack."""
    label = "slip@$100 p90 < net-edge p50 (worse slack)"
    sell = _side_slip_check(summary["sell"]["net_p50"],
                            summary["slip"]["sell"]["p90"])
    buy = _side_slip_check(summary["buy"]["net_p50"],
                           summary["slip"]["buy"]["p90"])
    if sell["slack"] is None or buy["slack"] is None:
        return {"value": None, "pass": False, "label": label,
                "sell": sell, "buy": buy}
    worst = min(sell["slack"], buy["slack"])
    return {"value": worst, "pass": worst > 0, "label": label,
            "sell": sell, "buy": buy}


def _g4_label() -> str:
    return (f"thin_frac < {THIN_FRAC_MAX:.2f} "
            f"(shallow book / sizing signal only)")


def _g4(summary: dict) -> dict:
    """Shallow-book sizing signal. PASS iff ``thin_frac < 0.30``.

    Mean ``depth_ok_frac`` is still reported. A missing depth column fails.
    """
    label = _g4_label()
    thin = summary["depth_thin_frac"]
    if not summary["depth_n"] or not _finite(thin):
        return {"value": None, "thin_frac": None, "pass": False,
                "label": label, "n": summary.get("depth_n") or 0}
    return {
        "value": summary["depth_mean"],
        "thin_frac": thin,
        "pass": thin < THIN_FRAC_MAX,
        "label": label,
        "n": summary["depth_n"],
    }


def evaluate_gates(summary: dict, sell_room: list, buy_room: list) -> dict:
    """Locked G1–G4 plus the SELL/BUY net p50 contrast columns."""
    return {
        "fees": summary["fees"],
        "basis": _basis_label(summary["used_fillable"]),
        "contrast": {
            "sell": summary["sell"]["net_p50"],
            "buy": summary["buy"]["net_p50"],
        },
        "G1": _g1(summary),
        "G2": _g2(sell_room, buy_room),
        "G3": _g3(summary),
        "G4": _g4(summary),
    }


def _gate_bps_line(gid: str, gate: dict) -> str:
    token = _fmt(gate["value"]) if gate["value"] is not None else "n/a"
    if token == "n/a":
        return f"{gid}: n/a {_verdict(gate['pass'])} {gate['label']}"
    return f"{gid}: {token} bps {_verdict(gate['pass'])} {gate['label']}"


def _shift_detail(name: str, side: dict) -> str:
    p90 = _fmt(side["p90"])
    shifts = side["shifts"]
    if not shifts:
        return f"  {name} p90 {p90} → n/a"
    return (f"  {name} p90 {p90} → {_fmt(shifts[0])} and {_fmt(shifts[1])}")


def _g4_line(g4: dict) -> str:
    """``G4: … PASS thin_frac=0.1200 (<0.30)`` or FAIL at ``thin_frac >= 0.30``."""
    cut = f"(<{THIN_FRAC_MAX:.2f})"
    verdict = _verdict(bool(g4["pass"]))
    if g4["value"] is None or not _finite(g4.get("thin_frac")):
        head = f"G4: n/a mean depth_ok_frac {verdict} thin_frac=n/a {cut}"
    else:
        head = (
            f"G4: {g4['value']:.4f} mean depth_ok_frac {verdict} "
            f"thin_frac={g4['thin_frac']:.4f} {cut}"
        )
    label = (g4.get("label") or "").strip()
    return f"{head} {label}" if label else head


def format_gate_report(gates: dict) -> str:
    """Stable stdout block. Gate lines start with ``G1:`` … ``G4:``.

    SELL/BUY net p50 are printed underneath as contrast, never as G1/G2.
    """
    g2 = gates["G2"]
    g3 = gates["G3"]
    g4 = gates["G4"]
    lines = [
        (f"Gates (rebate forced to 0, fees {gates['fees']:.1f} bps, "
         f"basis: {gates['basis']}):"),
        _gate_bps_line("G1", gates["G1"]),
        _gate_bps_line("G2", g2),
        _shift_detail("upper", g2["sides"]["upper"]),
        _shift_detail("lower", g2["sides"]["lower"]),
        _gate_bps_line("G3", g3),
    ]
    for name, side in (("SELL", g3["sell"]), ("BUY", g3["buy"])):
        lines.append(
            f"  {name} slip p90 {_fmt(side['slip'])} < net p50 {_fmt(side['net'])} "
            f"{_verdict(side['pass'])}"
        )
    lines.append(_g4_line(g4))
    lines.append("Contrast columns (not Gate ids):")
    lines.append(f"SELL entropy net p50: {_fmt(gates['contrast']['sell'])} bps")
    lines.append(f"BUY entropy net p50: {_fmt(gates['contrast']['buy'])} bps")
    return "\n".join(lines)


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
                        "G1 subtracts this from the fillable@$100 median with "
                        "rebate forced to 0. Pass a higher sum for a tradexyz hedge.")
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
    # accrual figure above is not an input. G2 shifts the p90 of this room.
    fees = args.fees_bps
    sell_room = firing_room(rows, "sell", midline, fees, used_fillable)
    buy_room = firing_room(rows, "buy", midline, fees, used_fillable)
    print(format_gate_report(evaluate_gates(summary, sell_room, buy_room)))
    print()
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
