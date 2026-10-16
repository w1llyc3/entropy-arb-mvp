"""End-to-end CLI workflow with injected transports, never real SDKs/accounts."""
import asyncio
from argparse import Namespace
from decimal import Decimal as D

import pytest

from entropy_arb.live_check import run
from entropy_arb.live_execution import LIVE_ACK
from entropy_arb.storage import RecoveryRequired


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    state=dict(hl=[],lighter=[],e=D(0),h=D(0),sends=0)
    env=tmp_path/'test.env'
    env.write_text('ENTROPY_HL_ACCOUNT=0x'+'1'*40+'\nLIGHTER_ACCOUNT_INDEX=7\n'
        'ENTROPY_LIVE_TEST=YES\nENTROPY_REVIEWED_COMMIT='+'a'*40+'\n'
        'LIGHTER_TRADE_FEE_SCALE=1000000\nLIGHTER_READ_TOKEN=synthetic\n')
    monkeypatch.setattr('entropy_arb.live_check.subprocess.check_output',
        lambda command,**kw:'a'*40 if command[1]=='rev-parse' else '')
    class Rest:
        def __init__(self,*a): self.token=None
        async def info(self,p):
            kind=p['type']
            if kind=='perpDexs': return [None,dict(name='io')]
            if kind=='meta': return dict(universe=[dict(name='io:SNDK',szDecimals=2)])
            if kind=='l2Book': return dict(levels=[[dict(px='1599.9',sz='1')],[dict(px='1600',sz='1')]])
            if kind=='openOrders': return []
            if kind=='clearinghouseState': return dict(withdrawable='1000',assetPositions=[dict(position=dict(coin='io:SNDK',szi=str(state['e'])))])
            if kind=='orderStatus':
                order=next((r for r in state['hl'] if r['cloid']==p['oid']),None)
                return dict(status='order',order=dict(status='filled',order=order)) if order else dict(status='unknownOid')
            if kind=='userFillsByTime': return [dict(coin='io:SNDK',oid=r['oid'],tid=r['oid'],sz=r['qty'],px=r['price'],fee='0.001',feeToken='USDC') for r in state['hl']]
            raise AssertionError(kind)
        async def exchange(self,p):
            assert p['action']['type']=='order'
            for wire in p['action']['orders']:
                state['sends']+=1
                state['e']+=D(wire['s'])*(1 if wire['b'] else -1)
                state['hl'].append(dict(coin='io:SNDK',cloid=wire['c'],oid=len(state['hl'])+1,timestamp=1,
                    qty=wire['s'],price=wire['p'],origSz=wire['s'],limitPx=wire['p'],side='B' if wire['b'] else 'A',reduceOnly=wire['r']))
            return dict(status='ok')
        async def lighter(self,path,p):
            if path=='orderBooks': return dict(order_books=[dict(symbol='SNDK',status='active',market_id=139,
                supported_size_decimals=4,supported_price_decimals=2,min_base_amount='.0035',min_quote_amount='10')])
            if path=='account': return dict(code=200,accounts=[dict(account_index=7,available_balance='1000',pending_order_count=0,
                positions=[dict(market_id=139,position=str(abs(state['h'])),sign=1 if state['h']>=0 else -1,pending_order_count=0)])])
            if path=='accountActiveOrders': return dict(code=200,orders=[])
            if path=='accountInactiveOrders': return dict(code=200,orders=state['lighter'],next_cursor='')
            if path=='orderBookOrders': return dict(code=200,bids=[dict(price='1599',remaining_base_amount='1')],asks=[dict(price='1599.1',remaining_base_amount='1')])
            if path=='trades':
                r=next(r for r in state['lighter'] if str(r['order_index'])==p['order_index_str'])
                ask=r['is_ask']; side='ask' if ask else 'bid'; other='bid' if ask else 'ask'
                trade=dict(trade_id=r['order_index'],market_id=139,is_maker_ask=not ask,
                    size=r['filled_base_amount'],price=r['price'],type='trade',taker_fee=1,maker_fee=0)
                trade.update({side+'_account_id':7,side+'_id':r['order_index'],side+'_client_id':r['client_order_index'],
                    other+'_account_id':8,other+'_id':42,other+'_client_id':42})
                return dict(code=200,trades=[trade],next_cursor='')
            raise AssertionError(path)
    class Signer:
        async def create_order(self,**kw):
            state['sends']+=1
            qty=D(kw['base_amount']).scaleb(-4)
            filled=qty*(state.pop('next_exit_fraction',D(1)) if kw['reduce_only'] else D(1))
            state['h']+=filled*(-1 if kw['is_ask'] else 1)
            state['lighter'].append(dict(owner_account_index=7,market_index=139,order_index=len(state['lighter'])+100,
                client_order_index=kw['client_order_index'],initial_base_amount=str(qty),filled_base_amount=str(filled),
                remaining_base_amount=str(qty-filled),status='filled' if filled==qty else 'canceled',
                is_ask=kw['is_ask'],price=str(D(kw['price']).scaleb(-2)),reduce_only=kw['reduce_only']))
            return None,None,None
        def create_auth_token_with_expiry(self): return 'synthetic',None
        async def close(self): pass
    async def signers(values):
        from types import SimpleNamespace
        return SimpleNamespace(address='0x'+'2'*40),lambda *a:{},Signer()
    monkeypatch.setattr('entropy_arb.live_check.RestTransport',Rest)
    monkeypatch.setattr('entropy_arb.live_check.make_signers',signers)
    def args(command,**kw):
        return Namespace(env_file=str(env),state_dir=str(tmp_path/'state'),command=command,ack=LIVE_ACK,**kw)
    return state,args,env


def test_manual_plan_send_replay_query_and_owned_close(runtime):
    state,args,_=runtime
    async def workflow():
        assert (await run(args('preflight')))['flat']
        plan=await run(args('prepare',key='entry',qty='.01',buy='entropy',price_entropy='1600',price_lighter='1599'))
        assert plan['sent'] is False and state['sends']==0
        op=plan['operation']['id']
        result=await run(args('send',operation=op))
        assert result['evidence_errors']==[] and state['sends']==2
        assert (await run(args('send',operation=op)))['replayed']
        assert state['sends']==2
        queried=await run(args('query'))
        assert queried['positions_observed_match'] and not queried['settlement_verified']
        close=await run(args('prepare-close',key='exit',price_entropy='1599.9',price_lighter='1599.1'))
        await run(args('send',operation=close['operation']['id']))
        assert state['sends']==4 and state['e']==state['h']==0
        queried=await run(args('query'))
        assert queried['positions_observed_match'] and not queried['settlement_verified']
        with pytest.raises(RecoveryRequired):
            await run(args('prepare',key='second-entry',qty='.01',buy='entropy',price_entropy='1600',price_lighter='1599'))
    asyncio.run(workflow())


@pytest.mark.parametrize('qty,price',[('.02','1600'),('.01','1700')])
def test_cap_and_price_drift_block_before_any_transport(runtime,qty,price):
    state,args,_=runtime
    with pytest.raises(RecoveryRequired):
        asyncio.run(run(args('prepare',key='entry',qty=qty,buy='entropy',price_entropy=price,price_lighter='1599')))
    assert state['sends']==0


def test_external_position_never_adopted(runtime):
    state,args,_=runtime
    state['e']=D('.01')
    with pytest.raises(RecoveryRequired):
        asyncio.run(run(args('prepare',key='entry',qty='.01',buy='entropy',price_entropy='1600',price_lighter='1599')))
    assert state['sends']==0


def test_disabled_live_gate_does_not_construct_signers(runtime,monkeypatch):
    state,args,env=runtime
    env.write_text(env.read_text().replace('ENTROPY_LIVE_TEST=YES','ENTROPY_LIVE_TEST=NO'))
    async def forbidden(*a): pytest.fail('signer loaded before opt-in')
    monkeypatch.setattr('entropy_arb.live_check.make_signers',forbidden)
    with pytest.raises(RecoveryRequired): asyncio.run(run(args('send',operation='missing')))
    assert state['sends']==0


def test_partial_exit_can_only_reduce_verified_remaining_position(runtime):
    state,args,_=runtime
    async def workflow():
        plan=await run(args('prepare',key='entry',qty='.01',buy='entropy',price_entropy='1600',price_lighter='1599'))
        await run(args('send',operation=plan['operation']['id']))
        close=await run(args('prepare-close',key='exit1',price_entropy='1599.9',price_lighter='1599.1'))
        state['next_exit_fraction']=D('.5')
        await run(args('send',operation=close['operation']['id']))
        assert state['e']==0 and state['h']==D('-.005')
        remaining=await run(args('prepare-close',key='exit2',price_entropy='1599.9',price_lighter='1599.1'))
        assert len(remaining['operation']['legs'])==1
        assert D(remaining['operation']['legs'][0]['qty'])==D('.005')
        await run(args('send',operation=remaining['operation']['id']))
        assert state['e']==state['h']==0 and state['sends']==5
    asyncio.run(workflow())


def test_manual_executor_cannot_overlap_one_service_account(runtime,tmp_path):
    from entropy_arb.service import TradingService
    from fakes.venues import FakeVenue
    state,args,_=runtime
    async def workflow():
        venues=dict(entropy=FakeVenue(venue='entropy',account='0x'+'1'*40),
                    lighter=FakeVenue(venue='lighter',account='99'))
        service=TradingService(tmp_path/'other-service',simulation=True,venues=venues,books={})
        await service.start()
        try:
            with pytest.raises(RecoveryRequired,match='overlapping'):
                await run(args('preflight'))
            assert state['sends']==0
        finally: await service.close()
    asyncio.run(workflow())
