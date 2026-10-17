"""Exact-identity attribution rules, independent of transports and order sending.

Callers must obtain claims from their durable pre-send intents, not browser input
or size matching. Unlinked exchange activity never becomes Session inventory.
"""
from decimal import Decimal


def reconcile(claims, orders, fills, *, positions=None, baseline=None, orders_complete=False):
    expected={}
    errors=[]
    for claim in claims:
        key=(claim["venue"],claim["account_domain"],claim["symbol"],claim["client_id"])
        if key in expected:
            errors.append("duplicate durable intent identity")
        expected[key]=claim
    for venue in ("entropy","lighter"):
        scoped=[c for c in claims if c["venue"]==venue]
        if len({(c["account_domain"],c["symbol"]) for c in scoped})>1:
            errors.append("mixed account or contract domains cannot be netted")
    matched={};foreign=[]
    for order in orders:
        key=(order["venue"],order["account_domain"],order["symbol"],order["client_id"])
        if key not in expected:
            foreign.append(order["client_id"])
            continue
        claim=expected[key]
        if key in matched or any(order.get(k)!=claim.get(k) for k in ("side","reduce_only","qty")):
            errors.append("order identity/payload conflict")
            continue
        matched[key]=order
        if order["state"] not in ("UNKNOWN","SUBMITTING","PREPARED","OPEN","PARTIAL","FILLED","CANCELED","REJECTED","NOT_SENT"):
            errors.append("unrecognized exchange order state")
        if order["state"] in ("UNKNOWN","SUBMITTING","PREPARED"):
            errors.append("order result unknown")
    qty={v:Decimal(0) for v in ("entropy","lighter")}
    totals={};seen={}
    for fill in fills:
        key=(fill["venue"],fill["account_domain"],fill["symbol"],fill["client_id"])
        if key not in expected:
            continue
        identity=(fill["venue"],fill["account_domain"],fill["trade_id"])
        if identity in seen:
            if seen[identity]!=fill:
                errors.append("conflicting duplicate trade")
            continue
        seen[identity]=fill
        amount=Decimal(str(fill["qty"]))
        price=Decimal(str(fill["price"]))
        if not amount.is_finite() or not price.is_finite() or amount<=0 or price<=0 or fill["side"]!=expected[key]["side"]:
            errors.append("invalid fill identity/quantity")
            continue
        totals[key]=totals.get(key,Decimal(0))+amount
        qty[fill["venue"]]+=amount*(1 if fill["side"] else -1)
    for key,claim in expected.items():
        if key not in matched:
            errors.append("owned order not found; outcome remains unknown")
            continue
        total=totals.get(key,Decimal(0))
        if total>Decimal(claim["qty"]) or total!=Decimal(str(matched[key]["filled"])):
            errors.append("cumulative order/fill mismatch")
        state=matched[key]["state"]
        if ((state=="FILLED" and total!=Decimal(claim["qty"]))
            or (state in ("REJECTED","NOT_SENT") and total!=0)
            or (state=="PARTIAL" and total<=0)):
            errors.append("terminal order/fill contradiction")
    if not orders_complete:
        errors.append("open-order coverage incomplete")
    if positions is None or baseline is None:
        errors.append("position or baseline unavailable")
    else:
        for venue in qty:
            if positions.get(venue) is None or baseline.get(venue) is None:
                errors.append("position unavailable")
            elif Decimal(str(positions[venue]))!=Decimal(str(baseline[venue]))+qty[venue]:
                errors.append("unattributed account inventory change")
    return dict(complete=not errors, errors=sorted(set(errors)), foreign_order_ids=foreign,
                owned_inventory={v:str(q) for v,q in qty.items()} if not errors else None,
                external_activity_adopted=False)
