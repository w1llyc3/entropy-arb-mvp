"""Analyzer: fillable@$100 gate, tob fallback, accrual is display-only.

Run:  python3 -m pytest tests/test_analyze.py
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.analyze import (  # noqa: E402
    load_rebate_assumptions, load_rows, summarize_fillable, firing_room,
    evaluate_gates, format_gate_report,
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
    text = format_gate_report(gates)
    assert "G1: +3.10 bps PASS" in text
    assert "G3:" in text and "FAIL" in text.split("G3:", 1)[1].splitlines()[0]
    assert "G4: 0.5000 mean depth_ok_frac thin_frac=0.3333" in text
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
