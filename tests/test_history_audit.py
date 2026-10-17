import asyncio
import copy
from types import SimpleNamespace
import pytest

from entropy_arb.history_audit import audit
from entropy_arb.storage import EvidenceConflict


def order():
    return dict(owner_account_index=10,market_index=55,is_ask=False,reduce_only=False,
                order_index=99,client_order_index=123,initial_base_amount='2',filled_base_amount='2',remaining_base_amount='0',price='100',status='filled')


def trade():
    return dict(trade_id=1,market_id=55,bid_account_id=10,bid_id=99,bid_client_id=123,
                is_maker_ask=True,type='trade',size='2',price='100',taker_fee=0,
                integrator_maker_fee=0,integrator_taker_fee=0)


class Rest:
    def __init__(self,fault=None):self.fault=fault;self.reads=0
    async def lighter(self,path,params):
        if path=='accountActiveOrders':return dict(code=200,orders=[])
        if path=='accountInactiveOrders':
            self.reads+=1
            r=order()
            if self.fault=='unstable' and self.reads>1:r['client_order_index']=456
            return dict(code=200,orders=[r],next_cursor='loop' if self.fault=='loop' else None)
        r=trade()
        if self.fault=='wrong-identity':r['bid_client_id']=456
        if self.fault=='missing':return dict(code=200,trades=[])
        if self.fault=='nonzero-fee':r['taker_fee']=100
        if self.fault=='missing-fee':del r['taker_fee']
        return dict(code=200,trades=[r,r])


def run(rest):
    return asyncio.run(audit(rest,dict(lighter='10'),dict(lighter=SimpleNamespace(market_id=55))))


def test_readonly_audit_matches_deduplicated_fills_without_finality_claim():
    result=run(Rest())
    assert result['sampled_cumulative_fills_verified'] and result['matched_unique_fills']==1
    assert result['zero_fee_sample_orders']==1 and not result['real_orders_sent']
    assert not result['complete_account_history_verified'] and not result['settlement_finality_verified']


@pytest.mark.parametrize('fault',['unstable','loop','wrong-identity','missing'])
def test_incomplete_or_conflicting_history_fails_closed(fault):
    with pytest.raises(EvidenceConflict):run(Rest(fault))


def test_nonzero_historical_fee_does_not_invent_scale():
    result=run(Rest('nonzero-fee'))
    assert result['zero_fee_sample_orders']==0 and not result['nonzero_fee_units_verified']


def test_missing_fees_never_pass_as_zero():
    result=run(Rest('missing-fee'))
    assert result['sampled_cumulative_fills_verified']
    assert not result['sampled_fee_fields_complete'] and result['zero_fee_sample_orders']==0
