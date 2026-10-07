"""Analyzer: fillable@$100 gate, tob fallback, accrual is display-only.

Run:  python3 -m pytest tests/test_analyze.py
"""
import csv
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.analyze import (  # noqa: E402
    load_rebate_assumptions, load_rows, summarize_fillable, firing_room,
    evaluate_gates, format_gate_report, resolve_midline,
    gates_versus_midline, assign_session, parse_sessions,
    is_us_eastern_dst, estimate_half_life, coverage_stats,
    format_coverage_report, format_session_report, configure_stdio,
    funding_diff_stats,
)

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _write(rows, header):
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


BASE = ["minute_ts", "time_utc", "entropy_bid", "entropy_ask",
        "hedge_bid", "hedge_ask", "premium_open_bps", "premium_high_bps",
        "premium_low_bps", "premium_close_bps", "premium_mean_bps",
        "premium_std_bps", "sell_edge_mean_bps", "sell_edge_max_bps",
        "buy_edge_mean_bps", "buy_edge_max_bps", "samples"]
FILL = BASE + ["fill_sell_edge_100_bps", "fill_buy_edge_100_bps",
               "slip_sell_100_bps", "slip_buy_100_bps", "depth_ok_frac"]


def _row(ts, prem, sell_max, buy_max, **extra):
    r = {k: "" for k in FILL}
    r.update({
        "minute_ts": ts, "time_utc": "t",
        "premium_close_bps": prem, "premium_mean_bps": prem,
        "sell_edge_max_bps": sell_max, "buy_edge_max_bps": buy_max,
        "samples": 60,
    })
    r.update(extra)
    return r


def test_fillable_gate_forces_rebate_zero_and_labels_p50():
    # three minutes of synthetic edges — not market data
    rows_in = [
        _row(1_700_000_000, 0, 10, 4,
             fill_sell_edge_100_bps=8, fill_buy_edge_100_bps=2,
             slip_sell_100_bps=1, slip_buy_100_bps=3, depth_ok_frac=1),
        _row(1_700_000_060, 0, 20, 6,
             fill_sell_edge_100_bps=12, fill_buy_edge_100_bps=4,
             slip_sell_100_bps=2, slip_buy_100_bps=4, depth_ok_frac=0.5),
        _row(1_700_000_120, 0, 30, 8,
             fill_sell_edge_100_bps="", fill_buy_edge_100_bps=6,
             slip_sell_100_bps="", slip_buy_100_bps=5, depth_ok_frac=0),
    ]
    path = _write(rows_in, FILL)
    rows, used = load_rows(path, hours=0, min_samples=10)
    assert used is True and len(rows) == 3
    assumptions = load_rebate_assumptions(os.path.join(ROOT, "config.yaml"))
    assert assumptions["referral_mode"] == "referred_t4"
    assert abs(assumptions["rebate_bps"] - 0.045) < 1e-9
    summary = summarize_fillable(rows, fees=0.9, rebate_bps=assumptions["rebate_bps"],
                                 used_fillable=True)
    # sell fillable observations are 8 and 12; median (p50) = 10
    assert abs(summary["sell"]["pre_p50"] - 10.0) < 1e-9
    assert abs(summary["sell"]["net_p50"] - (10.0 - 0.9)) < 1e-9
    assert abs(summary["buy"]["net_p50"] - (4.0 - 0.9)) < 1e-9
    assert abs(summary["sell"]["accrual_p50"]
               - (10.0 - 0.9 + 0.045)) < 1e-9
    # blank sell minute does not fire
    room = firing_room(rows, "sell", midline=0.0, fees=0.9, used_fillable=True)
    assert sum(1 for x in room if x >= 1.0) == 2
    assert abs(summary["depth_mean"] - (1 + 0.5 + 0) / 3) < 1e-9
    assert abs(summary["depth_thin_frac"] - (1 / 3)) < 1e-9
    assert summary["slip"]["sell"]["n"] == 2
    sell_room = firing_room(rows, "sell", midline=0.0, fees=0.9,
                            used_fillable=True)
    buy_room = firing_room(rows, "buy", midline=0.0, fees=0.9,
                           used_fillable=True)
    gates = evaluate_gates(summary, sell_room, buy_room)
    # G1 is the worse net median, not the SELL column.
    assert abs(gates["G1"]["value"] - (4.0 - 0.9)) < 1e-9
    assert gates["G1"]["pass"] is True
    assert "worse of SELL/BUY" in gates["G1"]["label"]
    assert gates["G2"]["pass"] is True
    assert gates["G2"]["value"] >= 0
    assert "±50%" in gates["G2"]["label"]
    # BUY slip p90 (4.8) is not < BUY net p50 (3.1), so G3 fails.
    assert gates["G3"]["pass"] is False
    assert gates["G3"]["value"] < 0
    assert "slip@$100 p90 < net-edge p50" in gates["G3"]["label"]
    assert abs(gates["G4"]["value"] - 0.5) < 1e-9
    assert abs(gates["G4"]["thin_frac"] - (1 / 3)) < 1e-9
    assert gates["G4"]["pass"] is False  # 1/3 >= 0.30
    assert "sizing signal" in gates["G4"]["label"]
    text = format_gate_report(gates)
    assert "G1: +3.10 bps PASS" in text
    assert "G3:" in text and "FAIL" in text.split("G3:", 1)[1].splitlines()[0]
    assert ("G4: 0.5000 mean depth_ok_frac FAIL thin_frac=0.3333 (<0.30)"
            in text)
    g1 = [ln for ln in text.splitlines() if ln.startswith("G1:")][0]
    g2 = [ln for ln in text.splitlines() if ln.startswith("G2:")][0]
    assert g1.startswith("G1: +3.10 bps PASS")
    assert "worse of SELL/BUY" in g1
    assert "±50%" in g2
    assert "SELL entropy net p50: +9.10 bps" in text
    assert "BUY entropy net p50: +3.10 bps" in text
    assert "GATE net p50" not in text
    # contrast lines are not gate ids
    for line in text.splitlines():
        if line.startswith("SELL entropy net") or line.startswith("BUY entropy net"):
            assert not line.startswith("G")


def test_missing_fillable_columns_fall_back_to_tob_max():
    header = list(BASE)
    path = _write([
        _row(1_700_000_000, 1.0, 5.0, -1.0),
    ], header)
    rows, used = load_rows(path, hours=0, min_samples=1)
    assert used is False
    summary = summarize_fillable(rows, fees=0.9, rebate_bps=0.045,
                                 used_fillable=False)
    assert abs(summary["sell"]["pre_p50"] - 5.0) < 1e-9
    assert abs(summary["sell"]["net_p50"] - 4.1) < 1e-9
    room = firing_room(rows, "sell", midline=0.0, fees=0.9, used_fillable=False)
    assert room == [5.0 - 0.9]


def test_g1_is_the_worse_median_and_zero_fails():
    rows_in = [
        _row(1, 0, 2, 2, fill_sell_edge_100_bps=0.9, fill_buy_edge_100_bps=2),
        _row(2, 0, 2, 2, fill_sell_edge_100_bps=0.9, fill_buy_edge_100_bps=2),
    ]
    path = _write(rows_in, FILL)
    rows, used = load_rows(path, hours=0, min_samples=1)
    summary = summarize_fillable(rows, fees=0.9, rebate_bps=0.0, used_fillable=used)
    gates = evaluate_gates(
        summary,
        firing_room(rows, "sell", 0.0, 0.9, True),
        firing_room(rows, "buy", 0.0, 0.9, True),
    )
    assert abs(gates["contrast"]["sell"]) < 1e-9
    assert gates["G1"]["value"] == gates["contrast"]["sell"]
    assert gates["G1"]["pass"] is False  # > 0 is required; zero is not positive


def test_g2_fails_when_shifted_band_fires_a_negative_room():
    # Midline sits above the sell edge, so sell room is negative while the
    # raw net median stays positive. G2 is the ±50% room test, not a p50.
    rows_in = [
        _row(1, 20, 10, 1, fill_sell_edge_100_bps=10, fill_buy_edge_100_bps=1,
             slip_sell_100_bps=0.1, slip_buy_100_bps=0.1, depth_ok_frac=1),
        _row(2, 20, 10, 1, fill_sell_edge_100_bps=10, fill_buy_edge_100_bps=1,
             slip_sell_100_bps=0.1, slip_buy_100_bps=0.1, depth_ok_frac=1),
    ]
    path = _write(rows_in, FILL)
    rows, _used = load_rows(path, hours=0, min_samples=1)
    summary = summarize_fillable(rows, fees=0.9, rebate_bps=0.0, used_fillable=True)
    sell_room = firing_room(rows, "sell", midline=20.0, fees=0.9, used_fillable=True)
    buy_room = firing_room(rows, "buy", midline=20.0, fees=0.9, used_fillable=True)
    gates = evaluate_gates(summary, sell_room, buy_room)
    assert gates["G1"]["pass"] is True
    assert gates["G1"]["value"] > 0
    assert gates["G2"]["pass"] is False
    assert gates["G2"]["value"] < 0
    text = format_gate_report(gates)
    assert "G2:" in text and "FAIL" in text.split("G2:", 1)[1].splitlines()[0]
    assert "BUY entropy net p50" in text
    assert "G2" not in text.split("Contrast columns", 1)[1]


def test_g3_passes_only_when_slip_p90_is_below_net_p50():
    rows_in = [
        _row(1, 0, 5, 5, fill_sell_edge_100_bps=5, fill_buy_edge_100_bps=5,
             slip_sell_100_bps=0.2, slip_buy_100_bps=0.2, depth_ok_frac=1),
        _row(2, 0, 5, 5, fill_sell_edge_100_bps=5, fill_buy_edge_100_bps=5,
             slip_sell_100_bps=0.4, slip_buy_100_bps=0.4, depth_ok_frac=1),
    ]
    path = _write(rows_in, FILL)
    rows, _used = load_rows(path, hours=0, min_samples=1)
    summary = summarize_fillable(rows, fees=0.9, rebate_bps=0.0, used_fillable=True)
    gates = evaluate_gates(
        summary,
        firing_room(rows, "sell", 0.0, 0.9, True),
        firing_room(rows, "buy", 0.0, 0.9, True),
    )
    assert gates["G3"]["sell"]["pass"] is True
    assert gates["G3"]["buy"]["pass"] is True
    assert gates["G3"]["pass"] is True
    assert gates["G3"]["value"] > 0
    report = format_gate_report(gates)
    g3 = [ln for ln in report.splitlines() if ln.startswith("G3:")][0]
    assert "PASS" in g3
    assert "p90" not in g3.split("bps", 1)[0]


def _summary_with_depth(depth_n, depth_mean, depth_thin_frac):
    return {
        "used_fillable": True,
        "fees": 0.9,
        "sell": {"n": 2, "pre_p50": 1.9, "net_p50": 1.0, "accrual_p50": 1.0},
        "buy": {"n": 2, "pre_p50": 1.9, "net_p50": 1.0, "accrual_p50": 1.0},
        "slip": {
            "sell": {"n": 2, "p90": 0.1},
            "buy": {"n": 2, "p90": 0.1},
        },
        "depth_n": depth_n,
        "depth_mean": depth_mean,
        "depth_thin_frac": depth_thin_frac,
    }


def _g4_line_of(depth_n, depth_mean, depth_thin_frac):
    gates = evaluate_gates(
        _summary_with_depth(depth_n, depth_mean, depth_thin_frac),
        [2.0, 2.0], [2.0, 2.0],
    )
    report = format_gate_report(gates)
    # G1–G3 stay the bps gates; this helper only returns the G4 line.
    assert any(ln.startswith("G1: ") and " bps " in ln for ln in report.splitlines())
    assert any(ln.startswith("G3: ") and " bps " in ln for ln in report.splitlines())
    return [ln for ln in report.splitlines() if ln.startswith("G4:")][0], gates["G4"]


def test_g4_passes_only_when_thin_frac_is_below_0_30():
    line, gate = _g4_line_of(25, 0.88, 0.12)
    assert gate["pass"] is True
    assert "PASS thin_frac=0.1200 (<0.30)" in line
    assert "thin_frac < 0.30 (shallow book / sizing signal only)" in line
    assert "KILL" not in line

    edge, edge_gate = _g4_line_of(10, 0.70, 0.30)
    assert edge_gate["pass"] is False
    assert "FAIL thin_frac=0.3000 (<0.30)" in edge

    above, above_gate = _g4_line_of(3, 0.5, 1 / 3)
    assert above_gate["pass"] is False
    assert "FAIL thin_frac=0.3333 (<0.30)" in above

    missing, missing_gate = _g4_line_of(0, float("nan"), float("nan"))
    assert missing_gate["pass"] is False
    assert missing_gate["value"] is None
    assert missing.startswith(
        "G4: n/a mean depth_ok_frac FAIL thin_frac=n/a (<0.30)")


def _ts(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp()


def test_half_life_on_contiguous_ar1_and_skips_gaps():
    # Exact y = 0.5 x, so phi is 0.5 and the half-life is one minute.
    points = []
    x = 1.0
    t = 1_700_000_000.0
    for _ in range(10):
        points.append((t, x))
        x *= 0.5
        t += 60.0
    t += 60.0  # 120s from the previous point: a gap, not a one-minute step
    x = 1.0
    for _ in range(10):
        points.append((t, x))
        x *= 0.5
        t += 60.0
    est = estimate_half_life(points)
    assert est["n_pairs"] == 18  # 9 + 9, the gap pair is dropped
    assert abs(est["phi"] - 0.5) < 1e-9
    assert abs(est["half_life_min"] - 1.0) < 1e-9
    assert est["reason"] is None

    short = estimate_half_life(points[:4])
    assert short["half_life_min"] is None
    assert "insufficient contiguous minute pairs" in short["reason"]
    assert "gaps are skipped" in short["reason"]

    explosive = []
    x = 1.0
    t = 0.0
    for _ in range(12):
        explosive.append((t, x))
        x *= 1.2
        t += 60.0
    bad = estimate_half_life(explosive)
    assert bad["half_life_min"] is None
    assert "non-mean-reverting" in bad["reason"]
    assert ">= 1" in bad["reason"]
    assert bad["phi"] >= 1.0

    oscillating = []
    x = 1.0
    t = 0.0
    for _ in range(12):
        oscillating.append((t, x))
        x *= -0.5
        t += 60.0
    neg = estimate_half_life(oscillating)
    assert neg["half_life_min"] is None
    assert "non-positive" in neg["reason"]

    flat = estimate_half_life([(i * 60.0, 0.0) for i in range(12)])
    assert flat["half_life_min"] is None
    assert "zero variance" in flat["reason"]


def test_session_split_tracks_us_dst_and_fixed_clocks():
    edt = datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc)
    est = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
    assert is_us_eastern_dst(edt) is True
    assert is_us_eastern_dst(est) is False
    # 2026-03-08 07:00 UTC is the second Sunday in March, 02:00 EST.
    assert is_us_eastern_dst(datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc)) is False
    assert is_us_eastern_dst(datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)) is True
    # 2026-11-01 06:00 UTC is the first Sunday in November, 02:00 EDT.
    assert is_us_eastern_dst(datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc)) is True
    assert is_us_eastern_dst(datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)) is False

    assert assign_session(_ts(2026, 7, 15, 13, 30)) == "us_regular"
    assert assign_session(_ts(2026, 7, 15, 13, 29)) == "asia"
    assert assign_session(_ts(2026, 7, 15, 19, 59)) == "us_regular"
    assert assign_session(_ts(2026, 7, 15, 20, 0)) == "us_post_overnight"
    assert assign_session(_ts(2026, 7, 15, 2, 59)) == "us_post_overnight"
    assert assign_session(_ts(2026, 7, 15, 3, 0)) == "asia"

    # January is EST: cash open is 14:30 UTC, not 13:30.
    assert assign_session(_ts(2026, 1, 15, 13, 30)) == "asia"
    assert assign_session(_ts(2026, 1, 15, 14, 30)) == "us_regular"
    assert assign_session(_ts(2026, 1, 15, 20, 59)) == "us_regular"
    assert assign_session(_ts(2026, 1, 15, 21, 0)) == "us_post_overnight"

    pinned = parse_sessions(
        "us_regular=13:30-20:00,us_post_overnight=20:00-03:00,asia=03:00-13:30")
    assert assign_session(_ts(2026, 1, 15, 13, 30), pinned) == "us_regular"
    assert assign_session(_ts(2026, 1, 15, 2, 0), pinned) == "us_post_overnight"
    assert assign_session(_ts(2026, 1, 15, 3, 0), pinned) == "asia"
    try:
        parse_sessions("nope")
        raise AssertionError("bad session spec should fail")
    except ValueError:
        pass


def test_coverage_warns_below_80pct_and_on_empty_hours():
    t0 = _ts(2026, 7, 15, 12, 0)
    rows = [{"ts": t0, "prem": 1.0}, {"ts": t0 + 2 * 3600, "prem": 1.0}]
    stats = coverage_stats(rows)
    assert stats["frac"] < 0.80
    assert any("UTC hour 13" in w and "Beijing 21" in w for w in stats["warnings"])
    assert any("overall coverage" in w and "below 0.80" in w for w in stats["warnings"])
    text = format_coverage_report(rows)
    assert "Beijing" in text and "UTC" in text
    assert "warning:" in text
    # an hour outside the 12:00-14:00 span is not a gap
    assert "UTC hour 05" not in text


def test_midline_auto_recomputes_gates_against_p50_not_zero():
    rows_in = [
        _row(1, -4, 0, 6, fill_sell_edge_100_bps=0, fill_buy_edge_100_bps=6,
             slip_sell_100_bps=0.1, slip_buy_100_bps=0.1, depth_ok_frac=1),
        _row(2, -4, 0, 6, fill_sell_edge_100_bps=0, fill_buy_edge_100_bps=6,
             slip_sell_100_bps=0.1, slip_buy_100_bps=0.1, depth_ok_frac=1),
    ]
    path = _write(rows_in, FILL)
    rows, used = load_rows(path, hours=0, min_samples=1)
    prem = sorted(r["prem"] for r in rows)
    mid, src = resolve_midline("auto", prem)
    assert src == "auto" and abs(mid - (-4.0)) < 1e-9
    zero, zsrc = resolve_midline("0", prem)
    assert zero == 0.0 and zsrc == "fixed"
    g_auto, _, _, _ = gates_versus_midline(rows, 0.9, 0.0, used, mid)
    g_zero, _, _, _ = gates_versus_midline(rows, 0.9, 0.0, used, zero)
    # sell net = 0 - (-4) - 0.9 = 3.1; buy net = 6 + (-4) - 0.9 = 1.1
    assert abs(g_auto["G1"]["value"] - 1.1) < 1e-9
    assert g_auto["G1"]["pass"] is True
    assert abs(g_zero["G1"]["value"] - (-0.9)) < 1e-9
    assert g_zero["G1"]["pass"] is False
    assert g_auto["G4"]["thin_frac"] == g_zero["G4"]["thin_frac"]
    assert g_auto["G4"]["pass"] is True
    text = format_gate_report(g_auto)
    assert text.split("G1:", 1)[1].splitlines()[0].find("PASS") >= 0
    assert "PASS" in text.split("G4:", 1)[1].splitlines()[0]
    assert "worse of SELL/BUY" in text
    assert "thin_frac < 0.30" in text


def _synthetic_minutes():
    """Two EDT sessions, a multi-hour gap, and funding on the cash session."""
    header = FILL + ["entropy_funding", "hedge_funding"]
    rows = []
    x = 1.0
    start = int(_ts(2026, 7, 15, 13, 30))
    for i in range(40):
        prem = -3.4 + x
        x *= 0.5
        rows.append(_row(
            start + i * 60, prem, prem + 2, -prem + 2,
            fill_sell_edge_100_bps=f"{prem + 2:.6f}",
            fill_buy_edge_100_bps=f"{-prem + 2:.6f}",
            slip_sell_100_bps="0.1", slip_buy_100_bps="0.1",
            depth_ok_frac="1",
            entropy_funding="0.0001", hedge_funding="-0.0002",
        ))
    night = int(_ts(2026, 7, 15, 20, 0))
    for i in range(12):
        prem = -5.5
        rows.append(_row(
            night + i * 60, prem, prem + 1, -prem + 1,
            fill_sell_edge_100_bps=f"{prem + 1:.6f}",
            fill_buy_edge_100_bps=f"{-prem + 1:.6f}",
            slip_sell_100_bps="0.2", slip_buy_100_bps="0.2",
            depth_ok_frac="0.5",
        ))
    return header, rows


def test_cli_by_session_prints_gates_coverage_and_funding(tmp_path):
    header, rows = _synthetic_minutes()
    path = _write(rows, header)
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "analyze.py"),
         "--csv", path, "--by-session", "--midline", "auto",
         "--fees-bps", "0.9", "--min-samples", "1",
         "--config", os.path.join(ROOT, "config.yaml")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "\u2212" not in out and "\u2212" not in proc.stderr
    assert "gate midline -" in out
    assert "Beijing" in out
    assert "warning: overall coverage" in out
    assert "has 0 minutes" in out
    assert "us_regular:" in out and "us_post_overnight:" in out
    assert "half-life" in out
    assert "funding diff" in out
    assert "0.0003" in out or "0.00030000" in out
    # Session gate lines are prefixed so the panel's G1: parser keeps the
    # overall block, which is printed unprefixed.
    assert any(ln.startswith("G1:") and ("PASS" in ln or "FAIL" in ln)
               for ln in out.splitlines())
    assert any(ln.startswith("G4:") and ("PASS" in ln or "FAIL" in ln)
               for ln in out.splitlines())
    assert any("[us_regular] G1:" in ln for ln in out.splitlines())
    assert any("[us_regular] G4:" in ln and ("PASS" in ln or "FAIL" in ln)
               for ln in out.splitlines())
    loaded, _used = load_rows(path, hours=0, min_samples=1)
    diff = funding_diff_stats(loaded)
    assert diff["n"] == 40
    assert abs(diff["mean"] - 0.0003) < 1e-12
    report = format_session_report(loaded, None, 0.9, 0.0, True)
    assert "n/a" in report
    assert "non-mean-reverting" in report or "half-life" in report


def test_configure_stdio_requests_utf8():
    class _Stream:
        def __init__(self):
            self.kwargs = None

        def reconfigure(self, **kwargs):
            self.kwargs = kwargs

    out, err = _Stream(), _Stream()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        configure_stdio()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    assert out.kwargs["encoding"] == "utf-8"
    assert out.kwargs["errors"] == "replace"
    assert err.kwargs["encoding"] == "utf-8"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
