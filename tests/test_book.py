"""plan_arb sizing math: thresholds, fees, caps, minimums.

Run:  python3 -m pytest tests/  (or  python3 tests/test_book.py)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import (  # noqa: E402
    OrderBook, plan_arb, size_above_min_notional, truncate_size,
)


def make_book(bids, asks):
    b = OrderBook()
    b.apply_hl([[{"px": str(p), "sz": str(s)} for p, s in bids],
                [{"px": str(p), "sz": str(s)} for p, s in asks]])
    return b


def common(**over):
    kw = dict(threshold_bps=0.0, buy_fee_bps=0.0, sell_fee_bps=0.0,
              take_fraction=1.0, cap_notional=1e9, min_base=0.0,
              min_notional=0.0, size_step=1e-4)
    kw.update(over)
    return kw


def test_no_edge_below_threshold():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps top
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=6.0))
    assert plan is None and reason == "no_edge"


def test_edge_above_threshold():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps top
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=4.0))
    assert reason == "ok"
    assert abs(plan.qty - 10.0) < 1e-9
    assert abs(plan.top_premium_bps - 5.0) < 0.01
    assert plan.exp_edge_usd > 0


def test_fees_kill_marginal_edge():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps gross
    # 3 + 3 bps of fees swallow the 5 bps premium
    plan, reason = plan_arb(buy, sell, **common(buy_fee_bps=3.0,
                                                sell_fee_bps=3.0))
    assert plan is None and reason == "no_edge"


def test_take_fraction_and_cap():
    buy = make_book(bids=[(99.9, 100)], asks=[(100.0, 100)])
    sell = make_book(bids=[(100.5, 100)], asks=[(100.6, 100)])
    plan, reason = plan_arb(buy, sell, **common(take_fraction=0.5))
    assert reason == "ok" and abs(plan.qty - 50.0) < 1e-9
    plan, reason = plan_arb(buy, sell, **common(cap_notional=1000.0))
    assert reason == "ok" and abs(plan.qty - 10.0) < 1e-6  # $1000 / $100


def test_min_notional():
    buy = make_book(bids=[(99.9, 0.05)], asks=[(100.0, 0.05)])
    sell = make_book(bids=[(100.5, 0.05)], asks=[(100.6, 0.05)])
    plan, reason = plan_arb(buy, sell, **common(min_notional=10.0))
    assert plan is None and reason == "below_min_notional"


def test_ceil_size_clears_ten_after_sz_decimals():
    # Incident: floor($10 / px) = 0.0061 and 0.0061 * px ≈ $9.96.
    # Hyperliquid then rejects "minimum value of $10" while Lighter fills.
    px = 1632.7868852459016
    floored = 0.0061
    assert floored * px < 10.0
    qty, reason = size_above_min_notional(
        floored,
        [(px * 0.995, 4), (px, 4)],
        min_notional=10.0,
        size_step=1e-4,
        target_notional=11.0,
    )
    assert reason == "ok" and qty is not None
    assert qty > floored
    for price, dec in ((px * 0.995, 4), (px, 4)):
        quote = truncate_size(qty, dec) * price
        assert quote + 1e-6 >= 11.0
        assert quote > 10.0
    # Coarse szDecimals cannot clear $11 without a jump to ~$100.
    # That must refuse so the hedge leg is never sent.
    refused, why = size_above_min_notional(
        0.11, [(100.0, 0), (100.0, 4)],
        min_notional=10.0, size_step=1e-4, target_notional=11.0,
    )
    assert refused is None and why == "below_min_notional"
    # A slice already well above the venue minimum stays put.
    kept, why = size_above_min_notional(
        1.0, [(100.0, 4), (100.0, 4)],
        min_notional=10.0, size_step=1e-4,
    )
    assert why == "ok" and kept == 1.0


def test_marginal_slice_respects_threshold():
    # second ask level only clears 2 bps — with threshold 3 the crossable
    # size must stop at the first level
    buy = make_book(bids=[(99.9, 5)], asks=[(100.0, 5), (100.08, 5)])
    sell = make_book(bids=[(100.1, 20)], asks=[(100.3, 20)])
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=3.0))
    assert reason == "ok"
    assert abs(plan.q_max - 5.0) < 1e-9


def test_lighter_diff_maintenance():
    b = make_book(bids=[(99.0, 5)], asks=[(100.0, 2), (100.1, 3)])
    # diff: the 100.0 ask level is removed server-side, a new bid appears
    b.apply_lighter({"bids": [{"price": "99.1", "size": "1"}],
                     "asks": [{"price": "100.0", "size": "0"}]},
                    snapshot=False)
    assert b.best_ask() == 100.1 and b.best_bid() == 99.1
    # a snapshot replaces the whole book
    b.apply_lighter({"bids": [{"price": "98.9", "size": "1"}],
                     "asks": [{"price": "100.2", "size": "3"}]},
                    snapshot=True)
    assert b.best_bid() == 98.9 and b.best_ask() == 100.2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
