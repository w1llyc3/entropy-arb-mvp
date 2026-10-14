"""One conservative risk gate shared by manual, automatic and reduction commands."""
from collections import deque
from decimal import Decimal
import time

from .book import walk_depth


class RiskRejected(RuntimeError): pass


class RiskEngine:
    def __init__(self, *, now=time.time):
        self.now = now
        self.sends = deque()

    def check(self, specs, snapshots, books, task, *, recovered, reduction=False):
        now = self.now()
        if not recovered and not reduction: raise RiskRejected("recovery gate closed")
        if set(snapshots) != {"entropy","lighter"}: raise RiskRejected("both accounts required")
        if any(not s.valid(now) for s in snapshots.values()): raise RiskRejected("stale account state")
        if any(s.open_orders for s in snapshots.values()): raise RiskRejected("orders outstanding")
        while self.sends and self.sends[0] < now-60: self.sends.popleft()
        if len(self.sends) >= 30: raise RiskRejected("order budget exhausted")
        for spec in specs:
            snapshot = snapshots[spec.venue]
            if snapshot.account != spec.account: raise RiskRejected("account mismatch")
            book = books.get(spec.venue)
            if book is None or not book.is_fresh(10): raise RiskRejected("stale order book")
            levels = book.sorted_asks() if spec.is_buy else book.sorted_bids()
            if sum(Decimal(str(size)) for _,size in levels) < spec.qty:
                raise RiskRejected("insufficient executable depth")
            marginal,_ = walk_depth(levels,float(spec.qty))
            if (spec.is_buy and Decimal(str(marginal)) > spec.limit_px) or (not spec.is_buy and Decimal(str(marginal)) < spec.limit_px):
                raise RiskRejected("approved price no longer executable")
            if reduction:
                available = max(-snapshot.position if spec.is_buy else snapshot.position,Decimal(0))
                if not spec.reduce_only or spec.qty > available: raise RiskRejected("reduce exceeds position")
            else:
                quote = spec.qty*spec.limit_px
                if not Decimal("10.5") <= quote <= Decimal(str(task["order_notional_usd"])):
                    raise RiskRejected("minimum or order cap violated after quantization")
                after = snapshot.position + spec.qty*(1 if spec.is_buy else -1)
                if abs(after)*spec.limit_px > Decimal(str(task["max_position_usd"])):
                    raise RiskRejected("gross position cap exceeded")
                if snapshot.available < quote*Decimal("1.00009"):
                    raise RiskRejected("available collateral insufficient")

    def record_send(self): self.sends.append(self.now())
