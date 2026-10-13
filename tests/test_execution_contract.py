import asyncio
from decimal import Decimal as D

import pytest

from entropy_arb.execution_contract import (
    BeforeSubmitError, OrderIntent, OrderReport, OrderState, submission_exception)
from fakes.venues import ExchangeState, FakeVenue


def intent(key="stable-1", **kw):
    return OrderIntent(key, D("0.01"), D("1600"), **({"is_buy": True} | kw))


@pytest.mark.parametrize("exc", [TimeoutError(), ConnectionError(), RuntimeError()])
def test_transport_exception_is_unknown(exc):
    assert submission_exception("id", exc).state == OrderState.UNKNOWN


def test_proven_pre_submit_error_is_not_sent():
    assert submission_exception("id", BeforeSubmitError()).state == OrderState.NOT_SENT


def test_lost_response_survives_client_restart_and_delayed_query():
    async def run():
        exchange = ExchangeState()
        old = FakeVenue(exchange, fault="lose_ack")
        with pytest.raises(TimeoutError):
            await old.submit(intent())
        new = FakeVenue(exchange, query_lag=1)
        assert (await new.query("stable-1")).state == OrderState.UNKNOWN
        assert (await new.query("stable-1")).filled == D("0.01")
        assert new.submit_calls == 0
        assert exchange.accepted == 1
        assert exchange.position == D("0.01")
    asyncio.run(run())


def test_partial_ioc_and_duplicate_events_keep_actual_fill():
    async def run():
        v = FakeVenue(fill_fraction=D("0.4"))
        r = await v.submit(intent())
        assert r.state == OrderState.CANCELED and r.filled == D("0.004")
        assert len(v.repeated_events()) == 3
        assert len({key for key, _ in v.repeated_events()}) == 1
        assert v.state.position == D("0.004")
    asyncio.run(run())


def test_reduce_only_never_opens_or_crosses_zero():
    async def run():
        v = FakeVenue(ExchangeState(position=D("-0.003")))
        assert (await v.submit(intent(reduce_only=True))).filled == D("0.003")
        assert v.state.position == 0
        assert (await v.submit(intent("reduce-2", reduce_only=True))).filled == 0
    asyncio.run(run())


def test_explicit_reject_and_not_found_are_different():
    async def run():
        v = FakeVenue(fault="reject")
        assert (await v.submit(intent())).state == OrderState.REJECTED
        assert (await v.query("absent")).state == OrderState.UNKNOWN
        assert v.state.accepted == 0
    asyncio.run(run())


def test_contract_rejects_invalid_and_unproven_reports():
    with pytest.raises(ValueError): intent("")
    with pytest.raises(ValueError): OrderIntent("x", D("NaN"), D(1), True)
    with pytest.raises(ValueError): OrderReport("x", OrderState.FILLED)
    with pytest.raises(ValueError): OrderReport("x", OrderState.REJECTED, D(1), True)
