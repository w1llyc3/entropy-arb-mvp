"""Minute recorder: aggregation, rollover, CSV output.

Run:  python3 -m pytest tests/  (or  python3 tests/test_recorder.py)
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.recorder import HEADER, MinuteRecorder, fillable_edge  # noqa: E402


def set_book(book, bid, ask):
    book.apply_hl([[{"px": str(bid), "sz": "10"}],
                   [{"px": str(ask), "sz": "10"}]])


def test_minute_aggregation_and_rollover():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)

    t0 = 1_700_000_000.0            # 20s into a minute (boundary at ...020)
    # minute 1: entropy 10 bps rich, then 20 bps rich
    set_book(e_book, 100.09, 100.11)   # mid 100.10
    set_book(h_book, 99.99, 100.01)    # mid 100.00
    rec.sample(t0)
    set_book(e_book, 100.19, 100.21)   # mid 100.20
    rec.sample(t0 + 10)
    # next minute: back to 10 bps rich -> flushes minute 1
    set_book(e_book, 100.09, 100.11)
    rec.sample(t0 + 45)
    rec.close()                        # flushes the partial minute 2

    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [*rows[0]] == HEADER
    assert len(rows) == 2
    m1, m2 = rows
    assert int(m1["samples"]) == 2 and int(m2["samples"]) == 1
    assert abs(float(m1["premium_open_bps"]) - 10.0) < 0.2
    assert abs(float(m1["premium_high_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_close_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_mean_bps"]) - 15.0) < 0.2
    # executable edges: sell = bid_e/ask_h - 1, buy = bid_h/ask_e - 1
    assert abs(float(m2["sell_edge_max_bps"])
               - ((100.09 / 100.01 - 1) * 1e4)) < 0.05
    assert abs(float(m2["buy_edge_max_bps"])
               - ((99.99 / 100.11 - 1) * 1e4)) < 0.05
    # closes carry the last books
    assert float(m2["entropy_bid"]) == 100.09
    assert float(m2["hedge_ask"]) == 100.01
    # single level, $1000 deep: fillable marginal edge == top of book, no slip
    tob_sell = (100.09 / 100.01 - 1.0) * 1e4
    tob_buy = (99.99 / 100.11 - 1.0) * 1e4
    assert abs(float(m2["fill_sell_edge_100_bps"]) - tob_sell) < 0.05
    assert abs(float(m2["fill_buy_edge_100_bps"]) - tob_buy) < 0.05
    assert abs(float(m2["slip_sell_100_bps"])) < 0.05
    assert abs(float(m2["slip_buy_250_bps"])) < 0.05
    assert float(m2["depth_ok_frac"]) == 1.0
    assert m2["entropy_funding"] == "" and m2["hedge_funding"] == ""


def test_stale_books_are_skipped():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
    rec.sample(1_700_000_000.0)        # both books empty -> nothing recorded
    set_book(e_book, 100.0, 100.02)    # only one side fresh
    rec.sample(1_700_000_001.0)
    rec.close()
    assert rec.rows_written == 0
    assert not os.path.exists(path)    # no row, no file


def _set_depth(book, bids, asks):
    book.apply_hl([[{"px": str(p), "sz": str(s)} for p, s in bids],
                   [{"px": str(p), "sz": str(s)} for p, s in asks]])


def test_thin_book_leaves_fillable_blank_but_still_records():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
    # $40 on each touch — $50/$100/$250 cannot fill. Row is still written.
    _set_depth(e_book, [(100.0, 0.4)], [(100.02, 0.4)])
    _set_depth(h_book, [(100.0, 0.4)], [(100.02, 0.4)])
    rec.sample(1_700_000_000.0)
    rec.close()
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    row = rows[0]
    assert row["fill_sell_edge_50_bps"] == ""
    assert row["fill_buy_edge_100_bps"] == ""
    assert row["slip_sell_250_bps"] == ""
    assert float(row["depth_ok_frac"]) == 0.0
    assert row["sell_edge_max_bps"] != ""
    assert int(row["samples"]) == 1


def test_fillable_walks_past_touch_and_funding_if_present():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
    # First $50 is at the touch; the next dollars are worse. $250 fits.
    _set_depth(e_book,
               [(101.0, 0.5), (100.0, 3.0)],
               [(100.0, 0.5), (100.5, 3.0)])
    _set_depth(h_book,
               [(100.0, 0.5), (99.5, 3.0)],
               [(100.0, 0.5), (100.5, 3.0)])
    e_book.funding = 0.000125
    h_book.funding = lambda: -0.0002
    t0 = 1_700_000_000.0
    rec.sample(t0)
    # second sample in the same minute has no $100 bid depth on entropy
    _set_depth(e_book, [(101.0, 0.2)], [(100.0, 5.0)])
    rec.sample(t0 + 1)
    rec.close()
    with open(path, newline="") as fh:
        row = list(csv.DictReader(fh))[0]
    assert int(row["samples"]) == 2
    # one of two samples could fill both directions at $100
    assert abs(float(row["depth_ok_frac"]) - 0.5) < 1e-9
    # $50 stays on the touch for the deep sample; the thin sample cannot,
    # so the minute mean is just the touch edge of the first sample.
    touch_sell = (101.0 / 100.0 - 1.0) * 1e4
    assert abs(float(row["fill_sell_edge_50_bps"]) - touch_sell) < 0.05
    assert abs(float(row["slip_sell_50_bps"])) < 0.05
    # $100 marginal edge is worse than the touch (walked into the second level)
    assert float(row["fill_sell_edge_100_bps"]) < touch_sell - 1.0
    assert float(row["slip_sell_100_bps"]) > 0.0
    assert row["fill_sell_edge_250_bps"] != ""
    assert row["entropy_funding"] != "" and row["hedge_funding"] != ""
    assert abs(float(row["entropy_funding"]) - 0.000125) < 1e-12
    assert abs(float(row["hedge_funding"]) - (-0.0002)) < 1e-12


def test_fillable_edge_insufficient_notional():
    sell = [(100.0, 0.4)]   # $40
    buy = [(100.0, 10.0)]
    assert fillable_edge(sell, buy, 50.0) == (None, None)
    edge, slip = fillable_edge([(100.0, 2.0)], [(100.0, 2.0)], 100.0)
    assert edge is not None and abs(edge) < 1e-9 and abs(slip) < 1e-9


def test_old_header_rotates():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    old = ("minute_ts,time_utc,entropy_bid,entropy_ask,hedge_bid,hedge_ask,"
           "premium_open_bps,premium_high_bps,premium_low_bps,"
           "premium_close_bps,premium_mean_bps,premium_std_bps,"
           "sell_edge_mean_bps,sell_edge_max_bps,buy_edge_mean_bps,"
           "buy_edge_max_bps,samples")
    with open(path, "w") as fh:
        fh.write(old + "\n1,2,3\n")
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
    rec.sample(1_700_000_000.0)
    rec.close()
    assert os.path.exists(path + ".old")
    with open(path) as fh:
        assert fh.readline().strip() == ",".join(HEADER)


def test_append_keeps_single_header():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    for start in (1_700_000_000.0, 1_700_000_060.0):
        rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9)
        rec.sample(start)
        rec.close()
    with open(path) as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == 3             # one header + two rows
    assert lines[0].startswith("minute_ts,")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
