"""Review-stage SNDK adapters. Importing this module never loads keys or sends.

Signers and transports are injected. A transport acknowledgement is UNKNOWN
until the exact order and complete fill history are queried. These adapters
are deliberately not wired into the default RECORD_ONLY web service.
"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import re
import time

from .account_state import TradeFill
from .execution_contract import BeforeSubmitError, OrderReport, OrderState
from .storage import EvidenceConflict

HL_REVISION = '2fdb18f9517675ea03695a0962bd19eece9c83f0'
LIGHTER_REVISION = '5825eb19867a4dbdd47c02642dc1eec30c30ca73'


def decimal(value):
    result=Decimal(str(value))
    if not result.is_finite(): raise EvidenceConflict('nonfinite exchange number')
    return result


def integer(value):
    result=decimal(value)
    if result!=result.to_integral_value(): raise EvidenceConflict('noninteger exchange identity')
    return int(result)


def plain(value):
    return format(value.normalize(),'f')


def managed_leg(ledger,venue,account,client_id):
    with ledger.lock:
        row=ledger.db.execute('SELECT * FROM legs WHERE venue=? AND account=? AND client_id=?',
                              (venue,account,client_id)).fetchone()
    if not row: raise EvidenceConflict('order is not in the managed ledger')
    return dict(row)


@dataclass(frozen=True)
class MarketRules:
    venue: str
    market_id: int
    size_decimals: int
    price_decimals: int
    min_base: Decimal
    min_quote: Decimal

    def validate(self,intent):
        if not 0<=self.size_decimals<=12 or not 0<=self.price_decimals<=12:
            raise BeforeSubmitError('unsupported precision')
        if intent.qty.scaleb(self.size_decimals)!=intent.qty.scaleb(self.size_decimals).to_integral_value():
            raise BeforeSubmitError('quantity off grid')
        if intent.limit_px.scaleb(self.price_decimals)!=intent.limit_px.scaleb(self.price_decimals).to_integral_value():
            raise BeforeSubmitError('price off grid')
        if self.venue=='entropy' and intent.limit_px!=intent.limit_px.to_integral_value():
            if len(intent.limit_px.normalize().as_tuple().digits)>5:
                raise BeforeSubmitError('HL price exceeds significant digits')
        if intent.qty<self.min_base or (not intent.reduce_only and intent.qty*intent.limit_px<self.min_quote):
            raise BeforeSubmitError('below exchange minimum')
        if self.venue=='lighter' and (intent.limit_px.scaleb(self.price_decimals)>2**32-1
                                      or intent.qty.scaleb(self.size_decimals)>2**63-1):
            raise BeforeSubmitError('wire integer overflow')


class ExternalIds:
    """Stable mapping committed before transport, collision checked per account.

    48 bits is a conservative application limit; account-specific acceptance
    must still be validated. Never allocate a replacement on retry/collision.
    """
    def __init__(self,ledger,account): self.ledger,self.account=ledger,str(account)

    def lighter(self,client_id):
        if not re.fullmatch('[0-9a-f]{32}',client_id): raise BeforeSubmitError('invalid stable ID')
        key='lighter-id:'+self.account+':'+client_id
        index=str(int.from_bytes(hashlib.sha256(client_id.encode()).digest()[:6],'big') or 1)
        reverse='lighter-id-owner:'+self.account+':'+index
        import json
        with self.ledger.transaction() as db:
            prior=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            if prior:
                old=json.loads(prior[0])
                if old!=index: raise EvidenceConflict('external ID mapping changed')
            owner=db.execute('SELECT value FROM settings WHERE key=?',(reverse,)).fetchone()
            if owner and json.loads(owner[0])!=client_id: raise EvidenceConflict('external ID collision')
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(key,json.dumps(index)))
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(reverse,json.dumps(client_id)))
        return int(index)


async def collect_pages(fetch,field,params,*,max_pages=100):
    """Bounded cursor traversal. Loops, API errors and missing shape fail closed."""
    rows=[]; seen=set(); cursor=None
    for _ in range(max_pages):
        page=await fetch(dict(params,**({'cursor':cursor} if cursor else {})))
        if integer(page.get('code',0))!=200 or not isinstance(page.get(field),list):
            raise EvidenceConflict('incomplete exchange history')
        rows.extend(page[field])
        cursor=page.get('next_cursor')
        if cursor in (None,''): return rows
        if not isinstance(cursor,str) or cursor in seen: raise EvidenceConflict('history cursor did not advance')
        seen.add(cursor)
    raise EvidenceConflict('history exceeds bounded pagination')


class LighterAdapter:
    is_simulation=False
    venue='lighter'

    def __init__(self,ledger,account,rules,get,*,signer=None,fee_scale=None):
        self.ledger,self.account,self.rules,self.get=ledger,str(account),rules,get
        self.signer=signer
        # Do not guess whether integer fee fields are USDC atomic units or a rate.
        self.fee_scale=fee_scale
        if fee_scale is not None and (not fee_scale.is_finite() or fee_scale<=0):
            raise ValueError('positive verified fee scale required')
        self.ids=ExternalIds(ledger,self.account)
        self.lock=asyncio.Lock()
        self.orders={}

    async def _get(self,path,params): return await self.get(path,params)

    async def submit(self,intent):
        self.rules.validate(intent)
        client_id=self.ids.lighter(intent.client_id)
        if self.signer is None: raise BeforeSubmitError('signer not configured')
        async with self.lock:
            # Official SDK allocates a nonce under its single configured key lock.
            # Never retry create_order here. Errors/acknowledgements are not fills.
            await self.signer.create_order(market_index=self.rules.market_id,
                client_order_index=client_id,base_amount=int(intent.qty.scaleb(self.rules.size_decimals)),
                price=int(intent.limit_px.scaleb(self.rules.price_decimals)),is_ask=not intent.is_buy,
                order_type=0,time_in_force=0,reduce_only=intent.reduce_only,order_expiry=0)
        return OrderReport(intent.client_id,OrderState.UNKNOWN,reason='submit requires order query')

    async def _find(self,client_id):
        external=self.ids.lighter(client_id)
        params=dict(account_index=int(self.account),market_id=self.rules.market_id)
        active=await self._get('accountActiveOrders',params)
        if integer(active.get('code',0))!=200 or not isinstance(active.get('orders'),list):
            raise EvidenceConflict('active order list incomplete')
        if active.get('next_cursor'): raise EvidenceConflict('active orders unexpectedly paginated')
        inactive=await collect_pages(lambda p:self._get('accountInactiveOrders',p),'orders',dict(params,limit=100))
        matches=[r for r in active['orders']+inactive if integer(r['client_order_index'])==external]
        # Multiple snapshots of one order can disagree across a race: retry reads,
        # never select whichever happens to look terminal.
        unique={str(integer(r['order_index'])):r for r in matches}
        if len(matches)!=1 or len(unique)!=1:
            if not matches: return None
            raise EvidenceConflict('ambiguous external order identity')
        order=matches[0]
        if integer(order['owner_account_index'])!=int(self.account) or integer(order['market_index'])!=self.rules.market_id:
            raise EvidenceConflict('order account or market mismatch')
        if type(order['is_ask']) is not bool: raise EvidenceConflict('order side unverified')
        leg=managed_leg(self.ledger,self.venue,self.account,client_id)
        if (decimal(order['initial_base_amount'])!=decimal(leg['qty']) or
            decimal(order['price'])!=decimal(leg['limit_px']) or order['is_ask']==bool(leg['is_buy']) or
            type(order.get('reduce_only')) is not bool or order['reduce_only']!=bool(leg['reduce_only'])):
            raise EvidenceConflict('exchange order differs from approved intent')
        self.orders[client_id]=order
        return order

    async def query(self,client_id):
        order=await self._find(client_id)
        if order is None: return OrderReport(client_id,OrderState.UNKNOWN,reason='not indexed; not proof of rejection')
        filled=decimal(order['filled_base_amount'])
        initial,remaining=decimal(order['initial_base_amount']),decimal(order['remaining_base_amount'])
        if min(filled,remaining)<0 or initial<=0 or filled>initial:
            raise EvidenceConflict('invalid cumulative order quantities')
        status=order['status']
        if status=='filled':
            if filled!=initial or remaining!=0: raise EvidenceConflict('filled amount differs from final quantities')
            state=OrderState.FILLED
        elif status=='canceled' or status in LIGHTER_CANCELED:
            state=OrderState.CANCELED
        elif status=='open':
            if filled+remaining!=initial: raise EvidenceConflict('open order quantities disagree')
            state=OrderState.PARTIAL if filled else OrderState.OPEN
        else: return OrderReport(client_id,OrderState.UNKNOWN,filled,reason='pending or unmapped status')
        return OrderReport(client_id,state,filled,True)

    async def query_fills(self,client_id):
        order=await self._find(client_id)
        if order is None: raise EvidenceConflict('order identity unresolved')
        params=dict(account_index=int(self.account),market_id=self.rules.market_id,
                    order_index_str=str(integer(order['order_index'])),sort_by='trade_id',limit=100,aggregate=False)
        trades=await collect_pages(lambda p:self._get('trades',p),'trades',params)
        fills={}
        for row in trades:
            ask=bool(order['is_ask']); side='ask' if ask else 'bid'
            if (integer(row['market_id'])!=self.rules.market_id or
                integer(row[side+'_account_id'])!=int(self.account) or
                integer(row[side+'_id'])!=integer(order['order_index']) or
                integer(row[side+'_client_id'])!=self.ids.lighter(client_id)):
                raise EvidenceConflict('trade identity mismatch')
            if type(row['is_maker_ask']) is not bool: raise EvidenceConflict('maker side unverified')
            role='maker' if row['is_maker_ask']==ask else 'taker'
            if self.fee_scale is None or row.get(role+'_fee') is None:
                raise EvidenceConflict('fee unit or amount not verified')
            qty,price=decimal(row['size']),decimal(row['price'])
            if qty<=0 or price<=0 or row.get('type')!='trade': raise EvidenceConflict('unsupported trade evidence')
            fee=decimal(row[role+'_fee'])/self.fee_scale
            trade_id=str(integer(row['trade_id']))
            fill=TradeFill(client_id,trade_id,qty,price,fee)
            if trade_id in fills and fills[trade_id]!=fill: raise EvidenceConflict('trade ID changed evidence')
            fills[trade_id]=fill
        if sum((f.qty for f in fills.values()),Decimal(0))!=decimal(order['filled_base_amount']):
            raise EvidenceConflict('fill history differs from cumulative order')
        return list(fills.values())

    async def cancel(self,client_id):
        order=await self._find(client_id)
        if order is None: raise EvidenceConflict('cannot cancel unresolved exchange order')
        if self.signer is None: raise BeforeSubmitError('signer not configured')
        async with self.lock:
            result=await self.signer.cancel_order(market_index=self.rules.market_id,
                                                order_index=integer(order['order_index']))
        if result[2] is not None: raise RuntimeError('cancel not acknowledged')


LIGHTER_CANCELED=frozenset('canceled-'+s for s in (
    'post-only','reduce-only','position-not-allowed','margin-not-allowed','too-much-slippage',
    'not-enough-liquidity','self-trade','expired','oco','child','liquidation','invalid-balance'))


class HyperliquidAdapter:
    is_simulation=False
    venue='entropy'

    def __init__(self,ledger,account,rules,info,post,*,wallet=None,sign=None):
        self.ledger,self.account,self.rules=ledger,account.lower(),rules
        self.info,self.post,self.wallet,self.sign=info,post,wallet,sign
        self.orders={}; self.lock=asyncio.Lock()

    def _cloid(self,client_id):
        if not re.fullmatch('[0-9a-f]{32}',client_id): raise BeforeSubmitError('invalid client ID')
        return '0x'+client_id

    async def _action(self,action):
        if self.wallet is None or self.sign is None: raise BeforeSubmitError('signer not configured')
        async with self.lock:
            with self.ledger.transaction() as db:
                import json
                address=getattr(self.wallet,'address',None)
                if not isinstance(address,str) or not re.fullmatch('0x[0-9a-fA-F]{40}',address):
                    raise BeforeSubmitError('signer identity unavailable')
                key='hl-nonce:'+address.lower()
                row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
                nonce=max(int(time.time()*1000),int(json.loads(row[0]))+1 if row else 0)
                if nonce>int(time.time()*1000)+2000: raise BeforeSubmitError('persisted nonce ahead of clock')
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(nonce)))
            expires=nonce+5000
            signature=self.sign(self.wallet,action,None,nonce,expires,True)
            return await self.post(dict(action=action,nonce=nonce,signature=signature,expiresAfter=expires))

    async def submit(self,intent):
        self.rules.validate(intent)
        wire=dict(a=self.rules.market_id,b=intent.is_buy,p=plain(intent.limit_px),s=plain(intent.qty),
                  r=intent.reduce_only,t=dict(limit=dict(tif='Ioc')),c=self._cloid(intent.client_id))
        await self._action(dict(type='order',orders=[wire],grouping='na'))
        return OrderReport(intent.client_id,OrderState.UNKNOWN,reason='submit requires order query')

    async def query(self,client_id):
        data=await self.info(dict(type='orderStatus',user=self.account,oid=self._cloid(client_id)))
        if data.get('status')!='order': return OrderReport(client_id,OrderState.UNKNOWN)
        outer=data['order']; order=outer['order']
        if order['coin']!='io:SNDK' or order.get('cloid','').lower()!=self._cloid(client_id):
            raise EvidenceConflict('HL order identity mismatch')
        leg=managed_leg(self.ledger,self.venue,self.account,client_id)
        if (decimal(order['origSz'])!=decimal(leg['qty']) or decimal(order['limitPx'])!=decimal(leg['limit_px'])
            or order['side']!=('B' if leg['is_buy'] else 'A') or
            type(order.get('reduceOnly')) is not bool or order['reduceOnly']!=bool(leg['reduce_only'])):
            raise EvidenceConflict('HL order differs from approved intent')
        self.orders[client_id]=order
        fills=await self.query_fills(client_id)
        filled=sum((f.qty for f in fills),Decimal(0))
        status=outer['status']
        state=(OrderState.FILLED if status=='filled' else OrderState.REJECTED if status in HL_REJECTED
               else OrderState.CANCELED if status in HL_CANCELED
               else (OrderState.PARTIAL if filled else OrderState.OPEN) if status=='open' else OrderState.UNKNOWN)
        return OrderReport(client_id,state,filled,state!=OrderState.UNKNOWN)

    async def query_fills(self,client_id):
        order=self.orders.get(client_id)
        if order is None: raise EvidenceConflict('HL order identity unresolved')
        # Fixed end boundary, bounded read: saturation remains incomplete rather
        # than guessing a timestamp cursor could skip same-millisecond fills.
        rows=await self.info(dict(type='userFillsByTime',user=self.account,
            startTime=integer(order['timestamp']),endTime=int(time.time()*1000),aggregateByTime=False))
        if not isinstance(rows,list) or len(rows)>=2000: raise EvidenceConflict('HL fill coverage unproven')
        fills={}
        for row in rows:
            if row['coin']!='io:SNDK' or integer(row['oid'])!=integer(order['oid']): continue
            if row.get('feeToken')!='USDC': raise EvidenceConflict('HL fee currency unverified')
            qty,price=decimal(row['sz']),decimal(row['px'])
            if qty<=0 or price<=0: raise EvidenceConflict('HL invalid fill')
            fill=TradeFill(client_id,str(integer(row['tid'])),qty,price,decimal(row['fee']))
            if fill.trade_id in fills and fills[fill.trade_id]!=fill: raise EvidenceConflict('HL trade ID changed evidence')
            fills[fill.trade_id]=fill
        return list(fills.values())

    async def cancel(self,client_id):
        response=await self._action(dict(type='cancelByCloid',cancels=[dict(asset=self.rules.market_id,
                                                                         cloid=self._cloid(client_id))]))
        if response.get('status')!='ok': raise RuntimeError('HL cancel not acknowledged')


HL_CANCELED=frozenset(('canceled','marginCanceled','vaultWithdrawalCanceled','openInterestCapCanceled',
    'selfTradeCanceled','reduceOnlyCanceled','siblingFilledCanceled','delistedCanceled',
    'liquidatedCanceled','scheduledCancel'))
HL_REJECTED=frozenset(('rejected','tickRejected','minTradeNtlRejected','perpMarginRejected',
    'reduceOnlyRejected','badAloPxRejected','iocCancelRejected','badTriggerPxRejected',
    'marketOrderNoLiquidityRejected','positionIncreaseAtOpenInterestCapRejected',
    'positionFlipAtOpenInterestCapRejected','tooAggressiveAtOpenInterestCapRejected',
    'openInterestIncreaseRejected','insufficientSpotBalanceRejected','oracleRejected','perpMaxPositionRejected'))
