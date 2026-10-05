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
    assert abs(summary["sell"]["gate_p50"] - (10.0 - 0.9)) < 1e-9
    assert abs(summary["sell"]["accrual_p50"]
               - (10.0 - 0.9 + 0.045)) < 1e-9
    # blank sell minute does not fire
    room = firing_room(rows, "sell", midline=0.0, fees=0.9, used_fillable=True)
    assert sum(1 for x in room if x >= 1.0) == 2
    assert abs(summary["depth_mean"] - (1 + 0.5 + 0) / 3) < 1e-9
    assert summary["slip"]["sell"]["n"] == 2


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
    assert abs(summary["sell"]["gate_p50"] - 4.1) < 1e-9
    room = firing_room(rows, "sell", midline=0.0, fees=0.9, used_fillable=False)
    assert room == [5.0 - 0.9]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
