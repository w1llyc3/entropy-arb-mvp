"""Bounded read-only historical SNDK contract audit. No intent adoption or sends.

An endpoint reaching its cursor end is only coverage of what that API exposes.
Matching a historical order's cumulative fills is not proof of new-order finality.
"""
import hashlib
import json
from decimal import Decimal as D

from .live_adapter import collect_pages, decimal, integer
from .storage import EvidenceConflict


def unique(rows, key):
    found={}
    for row in rows:
        identity=str(integer(row[key]))
        if identity in found and found[identity]!=row:
            raise EvidenceConflict('history duplicate changed evidence')
        found[identity]=row
    return found


def fingerprint(rows):
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate_order(order,account,market):
    required={'owner_account_index','market_index','is_ask','reduce_only','order_index','client_order_index','initial_base_amount','filled_base_amount','remaining_base_amount','price','status'}
    missing=required-set(order)
    if missing:raise EvidenceConflict('order schema missing fields: '+','.join(sorted(missing)))
    if integer(order['owner_account_index'])!=int(account) or integer(order['market_index'])!=market:
        raise EvidenceConflict('history account/market mismatch')
    if type(order['is_ask']) is not bool or type(order.get('reduce_only')) is not bool:
        raise EvidenceConflict('history side/reduce-only unavailable')
    for key in ('order_index','client_order_index'):integer(order[key])
    initial,filled,remaining=(decimal(order[k]) for k in ('initial_base_amount','filled_base_amount','remaining_base_amount'))
    if initial<=0 or min(filled,remaining)<0 or filled>initial or decimal(order['price'])<=0:
        raise EvidenceConflict('invalid historical quantities')
    if order['status']=='filled' and (filled!=initial or remaining!=0):
        raise EvidenceConflict('historical filled quantities conflict')


def match_trades(order,rows,account,market):
    side='ask' if order['is_ask'] else 'bid'
    trades=unique(rows,'trade_id')
    amount=D(0);zero_fees=True;fees_complete=True
    for row in trades.values():
        required={'market_id',side+'_account_id',side+'_id',side+'_client_id','is_maker_ask','type','size','price'}
        missing=required-set(row)
        if missing:raise EvidenceConflict('trade schema missing fields: '+','.join(sorted(missing)))
        if (integer(row['market_id'])!=market or integer(row[side+'_account_id'])!=int(account)
            or integer(row[side+'_id'])!=integer(order['order_index'])
            or integer(row[side+'_client_id'])!=integer(order['client_order_index'])):
            raise EvidenceConflict('historical fill identity mismatch')
        if type(row['is_maker_ask']) is not bool or row.get('type')!='trade':
            raise EvidenceConflict('historical fill role/type unavailable')
        qty,price=decimal(row['size']),decimal(row['price'])
        if qty<=0 or price<=0:raise EvidenceConflict('invalid historical fill')
        role='maker' if row['is_maker_ask']==order['is_ask'] else 'taker'
        fee_fields=(role+'_fee','integrator_maker_fee','integrator_taker_fee')
        if any(row.get(k) is None for k in fee_fields):
            fees_complete=False;zero_fees=False
        else:
            fee=decimal(row[role+'_fee'])
            integrator=[integer(row[k]) for k in ('integrator_maker_fee','integrator_taker_fee')]
            zero_fees=zero_fees and fee==0 and all(v==0 for v in integrator)
        amount+=qty
    if amount!=decimal(order['filled_base_amount']):
        raise EvidenceConflict('historical cumulative fills disagree')
    return dict(trades=len(trades),zero_fee_evidence=zero_fees,fee_fields_complete=fees_complete)


async def audit(rest,accounts,rules,*,max_pages=20,sample_orders=3):
    market=rules['lighter'].market_id
    params=dict(account_index=int(accounts['lighter']),market_id=market,limit=100)
    pages=0
    async def inactive(p):
        nonlocal pages
        pages+=1
        return await rest.lighter('accountInactiveOrders',p)
    async def stable_orders():
        rows=await collect_pages(inactive,'orders',params,max_pages=max_pages)
        found=unique(rows,'order_index')
        for row in found.values():validate_order(row,accounts['lighter'],market)
        return found
    before=await stable_orders()
    eligible=[r for r in before.values() if decimal(r['filled_base_amount'])>0 and
              (r['status']=='filled' or r['status']=='canceled' or str(r['status']).startswith('canceled-'))]
    # Bounded deterministic recent samples; no managed ledger rows are created.
    selected=sorted(eligible,key=lambda r:integer(r['order_index']),reverse=True)[:sample_orders]
    fills=0;zero=0;fee_complete=0
    for order in selected:
        query=dict(params,order_index_str=str(integer(order['order_index'])),sort_by='trade_id',aggregate=False)
        async def trades(p):
            nonlocal pages
            pages+=1
            return await rest.lighter('trades',p)
        rows=await collect_pages(trades,'trades',query,max_pages=max_pages)
        result=match_trades(order,rows,accounts['lighter'],market)
        fills+=result['trades'];zero+=int(result['zero_fee_evidence'])
        fee_complete+=int(result['fee_fields_complete'])
    after=await stable_orders()
    if fingerprint(before)!=fingerprint(after):
        raise EvidenceConflict('history changed during bounded audit; retry reads later')
    active=await rest.lighter('accountActiveOrders',dict(account_index=int(accounts['lighter']),market_id=market))
    if integer(active.get('code',0))!=200 or not isinstance(active.get('orders'),list) or active.get('next_cursor'):
        raise EvidenceConflict('active orders coverage unavailable')
    active_rows=unique(active['orders'],'order_index')
    for row in active_rows.values():validate_order(row,accounts['lighter'],market)
    return dict(mode='READ_ONLY_HISTORY_AUDIT',real_orders_sent=False,
        inactive_cursor_exhausted_twice=True,history_stable_across_reads=True,
        exposed_inactive_orders=len(before),active_orders=len(active_rows),
        sampled_orders=len(selected),matched_unique_fills=fills,
        zero_fee_sample_orders=zero,request_count=pages+1,
        sampled_fee_fields_complete=bool(selected) and fee_complete==len(selected),
        sampled_cumulative_fills_verified=bool(selected),
        exposed_active_order_coverage_verified=True,
        complete_account_history_verified=False,settlement_finality_verified=False,
        nonzero_fee_units_verified=False,automatic_live_enabled=False)
