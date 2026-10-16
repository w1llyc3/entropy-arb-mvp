"""Synthetic schema-based fixtures; NOT captured real authenticated responses."""
import asyncio
from decimal import Decimal as D
import json

import pytest

from entropy_arb.execution_contract import OrderIntent,OrderState,BeforeSubmitError
from entropy_arb.live_adapter import (MarketRules,ExternalIds,LighterAdapter,HyperliquidAdapter,
                                     collect_pages,LIGHTER_CANCELED)
from entropy_arb.live_execution import ManualLiveExecutor,LIVE_ACK
from entropy_arb.live_transport import read_operator_env
from entropy_arb.storage import Ledger,LegSpec,EvidenceConflict,RecoveryRequired


CLIENT='a'*32


@pytest.fixture
def ledger(tmp_path):
    store=Ledger(tmp_path/'ledger.sqlite')
    op=store.create_intent('fixture','fixture','open',[
        LegSpec('entropy','0x'+'1'*40,D('.01'),D('1600'),False),
        LegSpec('lighter','7',D('.01'),D('1600'),True)])
    store.db.execute('UPDATE legs SET client_id=? WHERE operation_id=?',(CLIENT,op))
    yield store
    store.close()


def rules(venue='lighter'):
    return MarketRules(venue,139 if venue=='lighter' else 110001,4,2,D('.0035'),D('10'))


def intent(**kw):
    return OrderIntent(CLIENT,D('.01'),D('1600'),True,**kw)


def order(ledger,**kw):
    return dict(client_order_index=ExternalIds(ledger,'7').lighter(CLIENT),order_index=12345678901234567,
        owner_account_index=7,market_index=139,filled_base_amount='0.004',initial_base_amount='0.01',
        remaining_base_amount='0.006',is_ask=False,status='canceled',price='1600',reduce_only=False)|kw


def trade(ledger,**kw):
    return dict(trade_id=99,market_id=139,ask_account_id=8,bid_account_id=7,ask_id=456,bid_id=12345678901234567,
        bid_client_id=ExternalIds(ledger,'7').lighter(CLIENT),ask_client_id=42,is_maker_ask=True,
        taker_fee=640,maker_fee=0,size='0.004',price='1600',type='trade')|kw


def adapter(ledger,row=None,trades=None,signer=None,fee_scale=D(1000000)):
    row=row or order(ledger)
    async def get(path,params):
        if path=='accountActiveOrders': return dict(code=200,orders=[])
        if path=='accountInactiveOrders': return dict(code=200,orders=[row],next_cursor='')
        assert params['order_index_str']==str(row['order_index'])
        return dict(code=200,trades=trades if trades is not None else [trade(ledger)],next_cursor='')
    return LighterAdapter(ledger,'7',rules(),get,signer=signer,fee_scale=fee_scale)


def test_external_mapping_survives_reopen_and_duplicate_key(ledger):
    first=ExternalIds(ledger,'7').lighter(CLIENT)
    assert 0<first<2**48
    other=Ledger(ledger.db.execute('PRAGMA database_list').fetchone()[2])
    try: assert ExternalIds(other,'7').lighter(CLIENT)==first
    finally: other.close()
    reverse='lighter-id-owner:7:'+str(first)
    ledger.set_setting(reverse,'different-client')
    with pytest.raises(EvidenceConflict): ExternalIds(ledger,'7').lighter(CLIENT)


@pytest.mark.parametrize('qty,px',[('0.00001','1600'),('.01','1600.001'),('.001','1600'),('.01','NaN')])
def test_wire_grid_and_minimum_fail_before_submit(qty,px):
    with pytest.raises((BeforeSubmitError,ValueError)):
        rules().validate(OrderIntent(CLIENT,D(qty),D(px),True))


def test_dust_reduce_avoids_open_min_notional_but_keeps_base_grid():
    rules().validate(OrderIntent(CLIENT,D('.004'),D('1600'),False,True))


def test_lighter_partial_cancel_and_exact_fill_identity(ledger):
    async def run():
        venue=adapter(ledger)
        report=await venue.query(CLIENT)
        fills=await venue.query_fills(CLIENT)
        assert report.state==OrderState.CANCELED and report.filled==D('.004')
        assert fills[0].fee==D('.00064')
        assert fills[0].trade_id=='99'
    asyncio.run(run())


@pytest.mark.parametrize('field,value',[('bid_account_id',8),('bid_id',9),('bid_client_id',9),('market_id',1)])
def test_lighter_rejects_fill_from_other_account_market_or_order(ledger,field,value):
    venue=adapter(ledger,trades=[trade(ledger,**{field:value})])
    with pytest.raises(EvidenceConflict): asyncio.run(venue.query_fills(CLIENT))


@pytest.mark.parametrize('field,value',[('initial_base_amount','.02'),('price','1601'),('is_ask',True),('reduce_only',True)])
def test_exact_client_id_with_changed_intent_is_not_adopted(ledger,field,value):
    with pytest.raises(EvidenceConflict):
        asyncio.run(adapter(ledger,row=order(ledger,**{field:value})).query(CLIENT))


@pytest.mark.parametrize('status',sorted(LIGHTER_CANCELED))
def test_documented_cancel_variants_keep_partial_quantity(ledger,status):
    report=asyncio.run(adapter(ledger,row=order(ledger,status=status)).query(CLIENT))
    assert report.state==OrderState.CANCELED and report.filled==D('.004')


def test_lighter_unknown_status_and_absent_history_are_not_rejected(ledger):
    assert asyncio.run(adapter(ledger,row=order(ledger,status='new-status')).query(CLIENT)).state==OrderState.UNKNOWN
    async def empty(path,params): return dict(code=200,orders=[])
    venue=LighterAdapter(ledger,'7',rules(),empty)
    assert asyncio.run(venue.query(CLIENT)).state==OrderState.UNKNOWN


def test_final_order_with_inconsistent_remaining_quantity_is_rejected(ledger):
    venue=adapter(ledger,row=order(ledger,status='filled',filled_base_amount='.01',remaining_base_amount='.001'))
    with pytest.raises(EvidenceConflict): asyncio.run(venue.query(CLIENT))


@pytest.mark.parametrize('trades',[[],None])
def test_incomplete_history_or_unknown_fee_units_blocks_release(ledger,trades):
    venue=adapter(ledger,trades=trades,fee_scale=None if trades is None else D(1000000))
    with pytest.raises(EvidenceConflict): asyncio.run(venue.query_fills(CLIENT))


def test_changed_duplicate_trade_id_is_rejected(ledger):
    venue=adapter(ledger,trades=[trade(ledger),trade(ledger,price='1601')])
    with pytest.raises(EvidenceConflict): asyncio.run(venue.query_fills(CLIENT))


def test_cursor_pagination_has_no_silent_truncation_or_loop():
    calls=[]
    async def fetch(params):
        calls.append(params)
        return dict(code=200,orders=[len(calls)],next_cursor='next' if len(calls)==1 else '')
    assert asyncio.run(collect_pages(fetch,'orders',{}))==[1,2]
    async def loop(params): return dict(code=200,orders=[],next_cursor='same')
    with pytest.raises(EvidenceConflict): asyncio.run(collect_pages(loop,'orders',{}))


def test_submit_uses_fixed_id_limit_ioc_and_ack_is_unknown(ledger):
    class Signer:
        calls=[]
        async def create_order(self,**kw):
            self.calls.append(kw)
            return None,object(),None
    signer=Signer(); venue=adapter(ledger,signer=signer)
    report=asyncio.run(venue.submit(intent()))
    assert report.state==OrderState.UNKNOWN
    assert signer.calls[0]['client_order_index']==ExternalIds(ledger,'7').lighter(CLIENT)
    assert signer.calls[0]['order_type']==0 and signer.calls[0]['time_in_force']==0
    assert signer.calls[0]['base_amount']==100 and signer.calls[0]['price']==160000


def test_cancel_uses_resolved_exchange_order_id(ledger):
    class Signer:
        calls=[]
        async def cancel_order(self,**kw): self.calls.append(kw); return None,None,None
    signer=Signer()
    asyncio.run(adapter(ledger,signer=signer).cancel(CLIENT))
    assert signer.calls==[dict(market_index=139,order_index=12345678901234567)]


def hl_adapter(ledger,info=None,post=None):
    from types import SimpleNamespace
    async def noop(data): return {}
    return HyperliquidAdapter(ledger,'0x'+'1'*40,rules('entropy'),info or noop,post or noop,
        wallet=SimpleNamespace(address='0x'+'2'*40),sign=lambda *args:{'r':'synthetic'})


def test_hl_wire_exact_decimal_stable_cloid_and_durable_nonce(ledger):
    posted=[]
    async def post(payload):
        posted.append(payload)
        assert ledger.get_setting('hl-nonce:0x'+'2'*40)==payload['nonce']
        return dict(status='ok')
    venue=hl_adapter(ledger,post=post)
    assert asyncio.run(venue.submit(intent())).state==OrderState.UNKNOWN
    wire=posted[0]['action']['orders'][0]
    assert wire['p']=='1600' and wire['s']=='0.01' and wire['c']=='0x'+CLIENT
    assert wire['t']==dict(limit=dict(tif='Ioc'))
    assert posted[0]['expiresAfter']-posted[0]['nonce']==5000


def test_hl_canceled_partial_uses_fills_not_remaining_size(ledger):
    async def info(payload):
        if payload['type']=='orderStatus':
            return dict(status='order',order=dict(status='canceled',order=dict(coin='io:SNDK',cloid='0x'+CLIENT,
                oid=9,timestamp=1,sz='0',origSz='.01',limitPx='1600',side='A',reduceOnly=False)))
        return [dict(coin='io:SNDK',oid=9,tid=100,sz='.004',px='1600',fee='-.01',feeToken='USDC')]
    report=asyncio.run(hl_adapter(ledger,info=info).query(CLIENT))
    assert report.state==OrderState.CANCELED and report.filled==D('.004')


def test_same_signer_across_accounts_uses_one_durable_nonce_sequence(ledger,monkeypatch):
    posted=[]
    async def post(payload): posted.append(payload); return dict(status='ok')
    first=hl_adapter(ledger,post=post)
    second=hl_adapter(ledger,post=post); second.account='0x'+'3'*40
    monkeypatch.setattr('entropy_arb.live_adapter.time.time',lambda:1800000000)
    async def run():
        await first.submit(intent()); await second.submit(intent())
    asyncio.run(run())
    assert posted[1]['nonce']==posted[0]['nonce']+1


def test_read_only_env_does_not_interpolate_or_load_private_values(tmp_path):
    path=tmp_path/'synthetic.env'
    path.write_text('ENTROPY_HL_PRIVATE_KEY=sentinel\nENTROPY_HL_ACCOUNT=${ENTROPY_HL_PRIVATE_KEY}\nLIGHTER_ACCOUNT_INDEX=7\n')
    values=read_operator_env(path)
    assert 'ENTROPY_HL_PRIVATE_KEY' not in values
    assert values['ENTROPY_HL_ACCOUNT']=='${ENTROPY_HL_PRIVATE_KEY}'


@pytest.mark.parametrize('change',[dict(enabled=False),dict(ack=None),dict(current_commit='b'*40),dict(reviewed_commit=None)])
def test_live_dispatcher_denies_default_or_changed_review(ledger,change):
    async def get(*args): return {}
    venues=dict(entropy=hl_adapter(ledger),lighter=LighterAdapter(ledger,'7',rules(),get))
    params=dict(enabled=True,ack=LIVE_ACK,reviewed_commit='a'*40,current_commit='a'*40)|change
    with pytest.raises(RecoveryRequired): ManualLiveExecutor(ledger,venues,**params)
