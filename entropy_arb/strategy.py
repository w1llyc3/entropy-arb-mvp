"""Existing band/depth math, driven by a service timer rather than HTTP GET."""
from decimal import Decimal

from .book import plan_arb, floor_step
from .storage import LegSpec


def candidate(task, books, snapshots, accounts):
    if set(books) != {"entropy","lighter"} or set(snapshots) != set(books): return None
    if any(not b.is_fresh(10) for b in books.values()): return None
    em, hm = books["entropy"].mid(),books["lighter"].mid()
    if not em or not hm: return None
    premium = (em/hm-1)*10000
    positions = {n:s.position for n,s in snapshots.items()}
    e,h = positions["entropy"],positions["lighter"]
    if e is None or h is None: return None
    if e or h:
        if e*h >= 0 or abs(e+h) > Decimal("0.001"): return None
        if not task["midline_bps"]-task["lower_bps"] <= premium <= task["midline_bps"]+task["upper_bps"]:
            return None
        buy,sell = ("entropy","lighter") if e<0 else ("lighter","entropy")
        qty = Decimal(str(floor_step(float(min(abs(e),abs(h))),.0001)))
        if qty <= 0: return None
        specs = [LegSpec(buy,accounts[buy],qty,Decimal(str(books[buy].best_ask())),True,True),
                 LegSpec(sell,accounts[sell],qty,Decimal(str(books[sell].best_bid())),False,True)]
        return dict(intent="close",direction="buy_entropy" if buy=="entropy" else "sell_entropy",
                    specs=specs,pre_fee=0.0,net=0.0)
    best = None
    for buy,sell,direction,threshold in (
        ("lighter","entropy","sell_entropy",task["midline_bps"]+task["upper_bps"]),
        ("entropy","lighter","buy_entropy",task["lower_bps"]-task["midline_bps"])):
        bf,sf = (0.9 if buy=="entropy" else 0),(0.9 if sell=="entropy" else 0)
        plan,_ = plan_arb(books[buy],books[sell],threshold_bps=threshold,buy_fee_bps=bf,
            sell_fee_bps=sf,take_fraction=.2,cap_notional=task["order_notional_usd"],
            min_base=.0001,min_notional=10.5,size_step=.0001)
        if not plan: continue
        specs=[LegSpec(buy,accounts[buy],Decimal(str(plan.qty)),Decimal(str(plan.buy_limit)),True),
               LegSpec(sell,accounts[sell],Decimal(str(plan.qty)),Decimal(str(plan.sell_limit)),False)]
        edge=plan.marginal_premium_bps-bf-sf
        if best is None or edge>best["net"]:
            best=dict(intent="open",direction=direction,specs=specs,
                      pre_fee=plan.marginal_premium_bps,net=edge)
    return best
