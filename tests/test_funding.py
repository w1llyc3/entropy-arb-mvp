"""Public funding parsers and the record-only poller. No network, no orders.

Run:  python3 -m pytest tests/test_funding.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.funding import (  # noqa: E402
    FundingPoller, parse_hl_funding, parse_lighter_funding,
)
from entropy_arb.venue_hl import HLVenue  # noqa: E402
from entropy_arb.venue_lighter import LighterVenue  # noqa: E402


HL_BODY = [
    {"universe": [
        {"name": "io:SNDK", "szDecimals": 2},
        {"name": "io:TSLA", "szDecimals": 2},
    ]},
    [
        {"funding": "0.0000125", "markPx": "100"},
        {"funding": "-0.0001", "markPx": "200"},
    ],
]

LIGHTER_BODY = {
    "code": 200,
    "funding_rates": [
        {"market_id": 42, "exchange": "hyperliquid", "symbol": "SNDK",
         "rate": 0.01},
        {"market_id": 42, "exchange": "lighter", "symbol": "SNDK",
         "rate": -0.0003},
        {"market_id": 7, "exchange": "lighter", "symbol": "AAPL",
         "rate": 0.2},
    ],
}


def test_parse_hl_funding_matches_dex_symbol_and_does_not_invent():
    rate, err = parse_hl_funding(HL_BODY, "io", "SNDK")
    assert err is None and abs(rate - 0.0000125) < 1e-15
    bare = [
        {"universe": [{"name": "SNDK"}]},
        [{"funding": "-0.0002"}],
    ]
    rate, err = parse_hl_funding(bare, "io", "SNDK")
    assert err is None and abs(rate - (-0.0002)) < 1e-15
    missing, why = parse_hl_funding(HL_BODY, "io", "AAPL")
    assert missing is None and "not in metaAndAssetCtxs" in why
    blank, why = parse_hl_funding(
        [{"universe": [{"name": "io:SNDK"}]}, [{"funding": ""}]], "io", "SNDK")
    assert blank is None and "blank" in why
    bad, why = parse_hl_funding({"nope": True}, "io", "SNDK")
    assert bad is None and why


def test_parse_lighter_funding_uses_lighter_row_only():
    rate, err = parse_lighter_funding(LIGHTER_BODY, "SNDK", 42)
    assert err is None and abs(rate - (-0.0003)) < 1e-15
    # without a market id, symbol match still ignores the hyperliquid row
    rate, err = parse_lighter_funding(LIGHTER_BODY, "SNDK", None)
    assert err is None and abs(rate - (-0.0003)) < 1e-15
    missing, why = parse_lighter_funding(LIGHTER_BODY, "SNDK", 99)
    assert missing is None and "market_id=99" in why
    # a wrong exchange must not be used even if the symbol matches
    only_hl = {"code": 200, "funding_rates": [
        {"market_id": 1, "exchange": "hyperliquid", "symbol": "SNDK",
         "rate": 0.5}]}
    missing, why = parse_lighter_funding(only_hl, "SNDK", None)
    assert missing is None and "symbol=SNDK" in why


def test_venue_fetch_uses_documented_payloads():
    class HLStub:
        api_url = "https://api.hyperliquid.xyz"

        def __init__(self):
            self.conf = type("C", (), {"hl_dex": "io", "symbol": "SNDK"})()
            self.payloads = []

        async def _info(self, payload):
            self.payloads.append(payload)
            return HL_BODY

    hl = HLStub()
    rate, err = asyncio.run(HLVenue.fetch_funding_rate(hl))
    assert hl.payloads == [{"type": "metaAndAssetCtxs", "dex": "io"}]
    assert err is None and abs(rate - 0.0000125) < 1e-15

    class HLDown(HLStub):
        async def _info(self, payload):
            raise TimeoutError("timed out")

    rate, err = asyncio.run(HLVenue.fetch_funding_rate(HLDown()))
    assert rate is None and "metaAndAssetCtxs" in err and "timed out" in err

    class LtStub:
        market_id = 42

        def __init__(self):
            self.conf = type("C", (), {"symbol": "SNDK"})()
            self.profile = type("P", (), {
                "api_url": "https://mainnet.zklighter.elliot.ai"})()
            self.paths = []

        async def _get(self, path, params=None):
            self.paths.append(path)
            return LIGHTER_BODY

    lt = LtStub()
    rate, err = asyncio.run(LighterVenue.fetch_funding_rate(lt))
    assert lt.paths == ["/api/v1/funding-rates"]
    assert err is None and abs(rate - (-0.0003)) < 1e-15


class _Venue:
    def __init__(self, result):
        self.book = OrderBook()
        self.result = result
        self.calls = 0

    async def fetch_funding_rate(self):
        self.calls += 1
        return self.result


def test_poller_stores_real_rates_and_keeps_last_on_failure():
    async def go():
        ent = _Venue((0.0001, None))
        hed = _Venue((-0.0002, None))
        poller = FundingPoller(ent, hed, interval_sec=60)
        await poller.poll_once()
        assert ent.book.funding == 0.0001
        assert hed.book.funding == -0.0002
        ent.result = (None, "POST /info metaAndAssetCtxs dex='io' failed: timeout")
        await poller.poll_once()
        assert ent.book.funding == 0.0001  # last real print, not a guess
        fresh = _Venue((None, "no exchange=lighter row"))
        await FundingPoller(fresh, hed, interval_sec=60).poll_once()
        assert fresh.book.funding is None

    asyncio.run(go())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
