"""Operator-run, isolated manual SNDK contract test. Never invoked by the web UI.

No default command sends. See MANUAL_LIVE_TEST.md before using send/cancel.
"""
import argparse
import asyncio
from decimal import Decimal as D
import json
from pathlib import Path
import re
import subprocess
import time

import aiohttp

from .account_state import AccountSnapshot
from .execution import DurableDispatcher
from .execution_contract import BeforeSubmitError
from .live_adapter import MarketRules,HyperliquidAdapter,LighterAdapter,decimal,integer
from .live_execution import ManualLiveExecutor,LIVE_ACK
from .live_transport import RestTransport,read_operator_env,make_signers,make_read_signer,inspect_operator_env,ExchangeReadError
from .ownership import OwnerLock,account_locks
from .storage import Ledger,LegSpec,RecoveryRequired,EvidenceConflict,TERMINAL


ROOT=Path(__file__).resolve().parents[1]


async def markets(rest):
    meta,dexes,books=await asyncio.gather(rest.info(dict(type='meta',dex='io')),
        rest.info(dict(type='perpDexs')),rest.lighter('orderBooks',{}))
    dex=[i for i,d in enumerate(dexes) if d and d.get('name')=='io']
    hl=[(i,a) for i,a in enumerate(meta['universe']) if a.get('name')=='io:SNDK']
    lighter=[b for b in books['order_books'] if b.get('symbol')=='SNDK' and b.get('status')=='active']
    if len(dex)!=1 or len(hl)!=1 or len(lighter)!=1: raise RecoveryRequired('ambiguous SNDK catalog')
    i,a=hl[0]; b=lighter[0]
    if a.get('isDelisted') or b.get('is_frozen'): raise RecoveryRequired('SNDK unavailable')
    sd=integer(a['szDecimals'])
    return dict(entropy=MarketRules('entropy',100000+dex[0]*10000+i,sd,max(0,6-sd),D(1).scaleb(-sd),D('10')),
                lighter=MarketRules('lighter',integer(b['market_id']),integer(b['supported_size_decimals']),
                    integer(b['supported_price_decimals']),decimal(b['min_base_amount']),decimal(b['min_quote_amount'])))


async def observe(rest,accounts,rules):
    started=time.time()
    hl,orders,account,active,hlbook,lbook=await asyncio.gather(
        rest.info(dict(type='clearinghouseState',user=accounts['entropy'],dex='io')),
        rest.info(dict(type='openOrders',user=accounts['entropy'],dex='io')),
        rest.lighter('account',dict(by='index',value=accounts['lighter'])),
        rest.lighter('accountActiveOrders',dict(account_index=int(accounts['lighter']),market_id=rules['lighter'].market_id)),
        rest.info(dict(type='l2Book',coin='io:SNDK')),
        rest.lighter('orderBookOrders',dict(market_id=rules['lighter'].market_id,limit=100)))
    if time.time()-started>5: raise RecoveryRequired('observation window too wide')
    positions=hl['assetPositions']
    if not isinstance(positions,list) or not isinstance(orders,list): raise RecoveryRequired('HL snapshot incomplete')
    hp=[p['position'] for p in positions if p['position']['coin']=='io:SNDK']
    if len(hp)>1: raise RecoveryRequired('duplicate HL position')
    if integer(account.get('code',0))!=200: raise RecoveryRequired('Lighter account incomplete')
    matches=[a for a in account['accounts'] if str(a['account_index'])==accounts['lighter']]
    if len(matches)!=1: raise RecoveryRequired('Lighter account identity mismatch')
    la=matches[0]; lp=[p for p in la['positions'] if integer(p['market_id'])==rules['lighter'].market_id]
    if len(lp)>1: raise RecoveryRequired('duplicate Lighter position')
    if integer(active.get('code',0))!=200 or not isinstance(active.get('orders'),list) or active.get('next_cursor'):
        raise RecoveryRequired('Lighter open order coverage incomplete')
    for order in active['orders']:
        if integer(order['owner_account_index'])!=int(accounts['lighter']) or integer(order['market_index'])!=rules['lighter'].market_id:
            raise RecoveryRequired('Lighter open order identity mismatch')
    if integer(la['pending_order_count']) or (lp and integer(lp[0]['pending_order_count'])):
        raise RecoveryRequired('pending account orders')
    if lp and integer(lp[0]['sign']) not in (-1,1): raise RecoveryRequired('invalid position sign')
    now=time.time()
    snapshots=dict(entropy=AccountSnapshot('entropy',accounts['entropy'],decimal(hp[0]['szi']) if hp else D(0),
        decimal(hl['withdrawable']),now,True,tuple(str(o['oid']) for o in orders)),
        lighter=AccountSnapshot('lighter',accounts['lighter'],decimal(lp[0]['position'])*integer(lp[0]['sign']) if lp else D(0),
        decimal(la['available_balance']),now,True,tuple(str(o['order_index']) for o in active['orders'])))
    if any(not s.valid(now) for s in snapshots.values()): raise RecoveryRequired('invalid account observation')
    # Evidence is a fresh observation, NOT a fabricated settlement watermark.
    e_bid=[(decimal(l['px']),decimal(l['sz'])) for l in hlbook['levels'][0]]
    e_ask=[(decimal(l['px']),decimal(l['sz'])) for l in hlbook['levels'][1]]
    if integer(lbook.get('code',0))!=200: raise RecoveryRequired('Lighter book incomplete')
    l_bid=[(decimal(l['price']),decimal(l['remaining_base_amount'])) for l in lbook['bids']]
    l_ask=[(decimal(l['price']),decimal(l['remaining_base_amount'])) for l in lbook['asks']]
    books=dict(entropy=(e_bid,e_ask),lighter=(l_bid,l_ask))
    for bid,ask in books.values():
        if not bid or not ask or max(p for p,s in bid)>min(p for p,s in ask): raise RecoveryRequired('empty or crossed book')
        if any(p<=0 or s<=0 for p,s in bid+ask): raise RecoveryRequired('invalid book level')
    return snapshots,books


async def check_read_contracts(rest,accounts,rules):
    """Read a bounded sample; report schema evidence, never auth or trade payloads."""
    params=dict(account_index=int(accounts['lighter']),market_id=rules['lighter'].market_id,limit=1)
    inactive,trades,hlfills=await asyncio.gather(
        rest.lighter('accountInactiveOrders',params),
        rest.lighter('trades',dict(params,sort_by='trade_id',aggregate=False)),
        rest.info(dict(type='userFillsByTime',user=accounts['entropy'],
            startTime=int((time.time()-86400)*1000),aggregateByTime=False)))
    for response,field in ((inactive,'orders'),(trades,'trades')):
        if integer(response.get('code',0))!=200 or not isinstance(response.get(field),list):
            raise RecoveryRequired('authenticated history response incomplete')
    for row in inactive['orders']:
        if integer(row['owner_account_index'])!=int(accounts['lighter']) or integer(row['market_index'])!=rules['lighter'].market_id:
            raise RecoveryRequired('history account or market mismatch')
        for key in ('client_order_index','order_index'): integer(row[key])
        for key in ('filled_base_amount','initial_base_amount','price'): decimal(row[key])
    for row in trades['trades']:
        if integer(row['market_id'])!=rules['lighter'].market_id or int(accounts['lighter']) not in (integer(row['ask_account_id']),integer(row['bid_account_id'])):
            raise RecoveryRequired('trade history identity mismatch')
        for key in ('trade_id','ask_id','bid_id'): integer(row[key])
        for key in ('size','price'): decimal(row[key])
    if not isinstance(hlfills,list): raise RecoveryRequired('HL history response incomplete')
    sndk=[r for r in hlfills if r.get('coin')=='io:SNDK']
    for row in sndk:
        for key in ('oid','tid'): integer(row[key])
        for key in ('sz','px','fee'): decimal(row[key])
        if row['feeToken']!='USDC': raise RecoveryRequired('HL fee currency unverified')
    return dict(mode='READ_ONLY_CONTRACT_CHECK',real_orders_sent=False,
        lighter_inactive_endpoint_ok=True,lighter_trade_endpoint_ok=True,hl_fill_endpoint_ok=True,
        lighter_order_sample_present=bool(inactive['orders']),lighter_trade_sample_present=bool(trades['trades']),
        hl_sndk_fill_sample_present=bool(sndk),
        more_lighter_history=bool(inactive.get('next_cursor') or trades.get('next_cursor')),
        history_completeness_verified=False,fee_unit_verified=False,settlement_contract_verified=False)


async def fee_guard(rest,accounts,scale,*,required=True):
    if scale is not None: return dict(policy='OPERATOR_VERIFIED_SCALE',allowed=True)
    limits=await rest.lighter('accountLimits',dict(account_index=int(accounts['lighter'])))
    if integer(limits.get('code',0))!=200: raise RecoveryRequired('account fee schedule unavailable')
    zero=all(integer(limits[key])==0 for key in ('current_maker_fee_tick','current_taker_fee_tick'))
    if required and not zero: raise RecoveryRequired('nonzero fee account requires verified fee units')
    # Exact zero is unit-independent. Do not infer a conversion scale or allow
    # nonzero fills; recheck this schedule immediately before every real send.
    return dict(policy='ZERO_ONLY',allowed=zero,nonzero_fee_units_verified=False)


async def check_hl_signer(rest,account,wallet):
    address=wallet.address.lower()
    if address==account: return 'MASTER'
    agents=await rest.info(dict(type='extraAgents',user=account))
    if not isinstance(agents,list): raise RecoveryRequired('HL signer authorization response incomplete')
    matches=[agent for agent in agents if str(agent.get('address','')).lower()==address]
    if len(matches)!=1 or integer(matches[0].get('validUntil',0))<=int(time.time()*1000)+60000:
        raise RecoveryRequired('HL signer is not a verified unexpired agent for this account')
    return 'APPROVED_AGENT'


def check_specs(specs,snapshots,books,rules):
    for leg in specs:
        rules[leg.venue].validate(DurableDispatcher.intent(leg.payload()|dict(client_id='0'*32)))
        if leg.qty*leg.limit_px>D(25): raise RecoveryRequired('$25 per-leg test cap')
        snapshot=snapshots[leg.venue]
        if not snapshot.valid(time.time()) or snapshot.open_orders: raise RecoveryRequired('stale or outstanding account state')
        bid,ask=books[leg.venue]; levels=ask if leg.is_buy else bid
        best=min(p for p,s in ask) if leg.is_buy else max(p for p,s in bid)
        if abs(leg.limit_px/best-1)>D('.0005'): raise RecoveryRequired('approved price is more than 5bp from touch')
        available=sum(s for p,s in levels if (p<=leg.limit_px if leg.is_buy else p>=leg.limit_px))
        if available<leg.qty: raise RecoveryRequired('insufficient depth inside approved limit')
        if leg.reduce_only:
            position=snapshot.position
            if leg.qty>max(-position if leg.is_buy else position,D(0)): raise RecoveryRequired('exit exceeds observed position')
        elif snapshot.available<leg.qty*leg.limit_px*D('1.1'):
            raise RecoveryRequired('insufficient conservative test collateral')


async def refresh_orders(ledger,adapters):
    # Do not invoke recover(): PREPARED plans are operator-reviewable, not lost work.
    errors=[]
    for op in ledger.all_operations():
        if op['state']=='ABORTED': continue
        for leg in op['legs']:
            if leg['submit_state']=='PREPARED': continue
            try:
                adapter=adapters[leg['venue']]
                report=await asyncio.wait_for(adapter.query(leg['client_id']),10)
                ledger.save_report(leg['id'],report)
                ledger.verify_cancellations(leg['id'])
                for fill in await asyncio.wait_for(adapter.query_fills(leg['client_id']),10):
                    ledger.record_fill(leg['id'],fill.trade_id,fill.qty,fill.price,fill.fee)
                if ledger.fill_total(leg['id'])!=report.filled: raise RecoveryRequired('fills disagree')
            except Exception as exc: errors.append(type(exc).__name__)
    return sorted(set(errors))


async def run(args):
    if args.command=='config-check':
        return dict(mode='CONFIG_PRESENCE_ONLY',real_orders_sent=False,**inspect_operator_env(args.env_file))
    mutating=args.command in ('send','cancel')
    signer_check=args.command=='signer-check'
    values=read_operator_env(args.env_file,secrets=False)
    authenticated_read=bool(getattr(args,'authenticated_read',False))
    if authenticated_read and not mutating:
        values=read_operator_env(args.env_file,lighter_auth=True)
    if signer_check: values=read_operator_env(args.env_file,secrets=True)
    accounts=dict(entropy=values.get('ENTROPY_HL_ACCOUNT','').lower(),lighter=values.get('LIGHTER_ACCOUNT_INDEX',''))
    if not re.fullmatch('0x[0-9a-f]{40}',accounts['entropy']) or not accounts['lighter'].isdigit():
        raise RecoveryRequired('public accounts missing or invalid')
    accounts['lighter']=str(int(accounts['lighter']))
    current=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    if mutating:
        if (values.get('ENTROPY_LIVE_TEST')!='YES' or values.get('ENTROPY_REVIEWED_COMMIT')!=current
            or args.ack!=LIVE_ACK): raise RecoveryRequired('review/opt-in/ack gate closed')
        if subprocess.check_output(['git','status','--porcelain','--untracked-files=normal'],cwd=ROOT,text=True).strip():
            raise RecoveryRequired('reviewed checkout must be clean')
        values=read_operator_env(args.env_file,secrets=True)
    state=Path(args.state_dir).resolve()
    owner=OwnerLock(dict(root=str(state)))
    account_owner=OwnerLock(dict(market='SNDK',deployment='io/lighter-mainnet',accounts=accounts))
    locks=account_locks(accounts)
    signer_owner=None
    if not owner.acquire(): raise RecoveryRequired('test state owned by another process')
    ledger=None; signer=None
    try:
        if not account_owner.acquire(): raise RecoveryRequired('accounts owned by another service')
        for lock in locks:
            if not lock.acquire(): raise RecoveryRequired('overlapping account owned by another service')
        state.mkdir(parents=True,exist_ok=True)
        ledger=Ledger(state/'ledger.sqlite')
        domain=dict(mode='MANUAL_LIVE_TEST',accounts=accounts)
        old=ledger.get_setting('account_domain')
        if old and old!=domain: raise RecoveryRequired('test ledger domain mismatch')
        ledger.set_setting('account_domain',domain)
        if args.command=='abort-plan':
            operation=ledger.operation(args.operation)
            if any(accounts.get(l['venue'])!=l['account'] for l in operation['legs']):
                raise RecoveryRequired('plan account identity mismatch')
            changed=ledger.abort_plan(args.operation)
            return dict(real_orders_sent=False,aborted=changed,operation=ledger.operation(args.operation))
        async with aiohttp.ClientSession() as session:
            rest=RestTransport(session,values.get('LIGHTER_READ_TOKEN'))
            wallet=sign=None
            if mutating or signer_check:
                wallet,sign,signer=await make_signers(values)
                signer_role=await check_hl_signer(rest,accounts['entropy'],wallet)
                signer_owner=OwnerLock(dict(signer=wallet.address.lower(),deployment='hyperliquid-mainnet'))
                if not signer_owner.acquire(): raise RecoveryRequired('HL signer owned by another executor')
            elif authenticated_read:
                signer=await make_read_signer(values)
            if signer:
                token,error=signer.create_auth_token_with_expiry(api_key_index=int(values['LIGHTER_API_KEY_INDEX']))
                if error: raise RecoveryRequired('authenticated read token unavailable')
                rest.token=token
            if signer_check:
                active=await rest.lighter('accountActiveOrders',dict(account_index=int(accounts['lighter'])))
                if integer(active.get('code',0))!=200 or not isinstance(active.get('orders'),list):
                    raise RecoveryRequired('Lighter read authentication response incomplete')
                return dict(mode='READ_ONLY_SIGNER_CHECK',hl_signer_authorized=True,hl_role=signer_role,
                    lighter_read_auth_ok=True,real_orders_sent=False,transaction_signatures_created=False)
            rules=await markets(rest)
            scale=decimal(values['LIGHTER_TRADE_FEE_SCALE']) if values.get('LIGHTER_TRADE_FEE_SCALE') else None
            if scale is not None and scale<=0: raise RecoveryRequired('invalid verified fee scale')
            adapters=dict(entropy=HyperliquidAdapter(ledger,accounts['entropy'],rules['entropy'],rest.info,rest.exchange,wallet=wallet,sign=sign),
                          lighter=LighterAdapter(ledger,accounts['lighter'],rules['lighter'],rest.lighter,signer=signer if mutating else None,fee_scale=scale))
            if args.command=='contract-check':
                result=await check_read_contracts(rest,accounts,rules)
                ledger.set_setting('read_contract_check',dict(result,observed_at=time.time()))
                return result
            if args.command=='preflight':
                snapshots,_=await observe(rest,accounts,rules)
                fees=await fee_guard(rest,accounts,scale,required=False)
                return dict(mode='READ_ONLY_PREFLIGHT',real_orders_sent=False,
                    flat=all(s.position==0 and not s.open_orders for s in snapshots.values()),
                    sdk_and_credentials_not_loaded=not authenticated_read,settlement_contract_verified=False,
                    fee_unit_configured=scale is not None,fee_policy=fees,automatic_live_enabled=False)
            if args.command=='query':
                errors=await refresh_orders(ledger,adapters)
                snapshots,_=await observe(rest,accounts,rules)
                expected={n:D(0) for n in accounts}
                for op in ledger.all_operations():
                    for l in op['legs']: expected[l['venue']]+=D(l['filled'])*(1 if l['is_buy'] else -1)
                return dict(operations=ledger.all_operations(),evidence_errors=errors,
                    observed_positions={n:str(s.position) for n,s in snapshots.items()},
                    positions_observed_match=all(s.position==expected[n] for n,s in snapshots.items()),
                    settlement_verified=False,automatic_live_enabled=False)
            if args.command in ('prepare','prepare-close'):
                await fee_guard(rest,accounts,scale)
                errors=await refresh_orders(ledger,adapters)
                if errors: raise RecoveryRequired('order/fill evidence incomplete')
                snapshots,books=await observe(rest,accounts,rules)
                operations=[o for o in ledger.all_operations() if o['state']!='ABORTED']
                specs=[]
                if args.command=='prepare':
                    if operations: raise RecoveryRequired('only one entry permitted per test ledger; query existing work')
                    if any(s.position or s.open_orders for s in snapshots.values()): raise RecoveryRequired('dedicated flat SNDK accounts required')
                    qty=decimal(args.qty)
                    for name in accounts:
                        specs.append(LegSpec(name,accounts[name],qty,decimal(getattr(args,'price_'+name)),name==args.buy))
                    intent='open'
                else:
                    if (not operations or len(operations)>=5 or sum(o['intent']=='open' for o in operations)!=1
                        or any(o['intent'] not in ('open','reduce') for o in operations)):
                        raise RecoveryRequired('one entry and at most four explicit exits permitted')
                    expected={n:D(0) for n in accounts}
                    for operation in operations:
                        for leg in operation['legs']:
                            if leg['outcome'] not in TERMINAL or ledger.fill_total(leg['id'])!=D(leg['filled']):
                                raise RecoveryRequired('unknown or incomplete order; do not blindly replace orders')
                            expected[leg['venue']]+=D(leg['filled'])*(1 if leg['is_buy'] else -1)
                    for name,snapshot in snapshots.items():
                        if snapshot.position!=expected[name]: raise RecoveryRequired('external or unexplained position; no automatic adoption')
                        if snapshot.position:
                            specs.append(LegSpec(name,accounts[name],abs(snapshot.position),decimal(getattr(args,'price_'+name)),snapshot.position<0,True))
                    if not specs: raise RecoveryRequired('already observed flat')
                    intent='reduce'
                check_specs(specs,snapshots,books,rules)
                if intent=='reduce':
                    # Explicit operator-requested exit after exact order/fill/position
                    # agreement. Never marks settlement or releases reservations.
                    with ledger.transaction() as db:
                        for operation in operations:
                            db.execute("UPDATE operations SET state='NEEDS_REDUCTION' WHERE id=?",(operation['id'],))
                            ledger._event(db,operation['id'],None,'OPERATOR_TEST_EXIT_VERIFIED')
                op=ledger.create_intent(args.key,args.key,intent,specs,baselines=snapshots)
                ledger.set_setting('manual-test-plan:'+op,dict(commit=current,created=time.time()))
                # Map external IDs now, not after order acceptance.
                for leg in ledger.operation(op)['legs']:
                    if leg['venue']=='lighter': adapters['lighter'].ids.lighter(leg['client_id'])
                return dict(sent=False,operation=ledger.operation(op),expires_in_sec=600)
            executor=ManualLiveExecutor(ledger,adapters,enabled=True,ack=args.ack,
                reviewed_commit=values.get('ENTROPY_REVIEWED_COMMIT'),current_commit=current)
            if args.command=='send':
                op=ledger.operation(args.operation)
                if op['state']!='INTENT_COMMITTED': return dict(sent=False,replayed=True,operation=op)
                plan=ledger.get_setting('manual-test-plan:'+args.operation)
                if not plan or plan['commit']!=current or not 0<=time.time()-plan['created']<=600:
                    raise RecoveryRequired('plan missing, stale, or code changed')
                await fee_guard(rest,accounts,scale)
                # Reject a pre-existing external ID before either leg can send.
                # This is not an assertion that the exchange deduplicates IDs.
                for leg in op['legs']:
                    if leg['venue']=='lighter':
                        if await adapters['lighter']._find(leg['client_id']) is not None:
                            raise RecoveryRequired('external client ID already exists; query, never replace')
                    else:
                        prior=await rest.info(dict(type='orderStatus',user=accounts['entropy'],oid='0x'+leg['client_id']))
                        if prior.get('status')!='unknownOid':
                            raise RecoveryRequired('HL client ID lookup not proven unused')
                snapshots,books=await observe(rest,accounts,rules)
                specs=[LegSpec(l['venue'],l['account'],D(l['qty']),D(l['limit_px']),bool(l['is_buy']),bool(l['reduce_only'])) for l in op['legs']]
                for leg in specs:
                    baseline=ledger.db.execute('SELECT payload FROM baselines WHERE operation_id=? AND venue=?',(op['id'],leg.venue)).fetchone()
                    if not baseline or snapshots[leg.venue].position!=D(json.loads(baseline[0])['position']):
                        raise RecoveryRequired('position changed since plan; query before sending')
                check_specs(specs,snapshots,books,rules)
                await asyncio.wait_for(executor.execute(args.operation),12)
                errors=await refresh_orders(ledger,adapters)
                return dict(operation=ledger.operation(args.operation),evidence_errors=errors,settlement_verified=False)
            if args.command=='cancel':
                with ledger.lock:
                    leg=ledger.db.execute('SELECT * FROM legs WHERE id=?',(args.leg,)).fetchone()
                if not leg or accounts.get(leg['venue'])!=leg['account']: raise RecoveryRequired('managed leg required')
                result=await executor.cancel(args.key,args.leg)
                errors=await refresh_orders(ledger,adapters)
                return dict(cancellation=result,evidence_errors=errors,settlement_verified=False)
    finally:
        try:
            if signer: await signer.close()
        finally:
            try:
                if ledger: ledger.close()
            finally:
                if signer_owner: signer_owner.close()
                for lock in locks: lock.close()
                account_owner.close(); owner.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',default=str(ROOT/'.env.live'))
    parser.add_argument('--state-dir',default=str(ROOT/'.web/manual-live-test'))
    parser.add_argument('--authenticated-read',action='store_true',
        help='explicitly load only Lighter auth credentials for read-only commands; never submit')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('config-check')
    sub.add_parser('preflight'); sub.add_parser('query'); sub.add_parser('contract-check'); sub.add_parser('signer-check')
    p=sub.add_parser('abort-plan'); p.add_argument('--operation',required=True)
    for name in ('prepare','prepare-close'):
        p=sub.add_parser(name); p.add_argument('--key',required=True)
        p.add_argument('--price-entropy',required=True); p.add_argument('--price-lighter',required=True)
        if name=='prepare': p.add_argument('--qty',required=True); p.add_argument('--buy',choices=['entropy','lighter'],required=True)
    p=sub.add_parser('send'); p.add_argument('--operation',required=True); p.add_argument('--ack',required=True)
    p=sub.add_parser('cancel'); p.add_argument('--leg',required=True); p.add_argument('--key',required=True); p.add_argument('--ack',required=True)
    args=parser.parse_args(argv)
    try:
        # Avoid SDK debug logs containing signed transaction or auth material.
        import logging
        logging.disable(logging.CRITICAL)
        result=asyncio.run(run(args))
        print(json.dumps(result,ensure_ascii=False,default=str))
    except Exception as exc:
        # No raw exceptions or traceback: parsers/SDKs can include credentials.
        message=str(exc) if type(exc) in (RecoveryRequired,EvidenceConflict,BeforeSubmitError) else 'test blocked; inspect configuration and sanitized evidence'
        detail=dict(endpoint=exc.endpoint,http_status=exc.status,category=exc.category) if isinstance(exc,ExchangeReadError) else {}
        print(json.dumps(dict(ok=False,error_type=type(exc).__name__,message=message,**detail)))
        return 1
    return 0


if __name__=='__main__': raise SystemExit(main())
