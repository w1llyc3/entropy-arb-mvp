"""Exchange state survives replacement of the fake client, not disk/process.

This venue explicitly models client-ID deduplication. It does NOT establish
that HL or Lighter implements equivalent semantics.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from entropy_arb.execution_contract import OrderIntent, OrderReport, OrderState


@dataclass
class ExchangeState:
    intents: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    position: Decimal = Decimal(0)
    accepted: int = 0


class FakeVenue:
    is_simulation = True
    def __init__(self, state=None, *, fault=None, fill_fraction=Decimal(1),
                 query_lag=0, venue="entropy", account="fake-account-e"):
        self.venue,self.account=venue,account
        self.state = state if state is not None else ExchangeState()
        self.fault = fault
        self.fill_fraction = fill_fraction
        if not 0 <= fill_fraction <= 1:
            raise ValueError("invalid fraction")
        self.query_lag = query_lag
        self.submit_calls = 0
        self.cancel_calls = 0

    async def submit(self, intent: OrderIntent):
        self.submit_calls += 1
        prior = self.state.intents.get(intent.client_id)
        if prior:
            if prior != intent:
                raise ValueError("client ID reused with different payload")
            return self.state.reports[intent.client_id]
        if self.fault == "reject":
            report = OrderReport(intent.client_id, OrderState.REJECTED,
                                 authoritative=True)
        else:
            filled = intent.qty * self.fill_fraction
            if intent.reduce_only:
                reducible = max(-self.state.position if intent.is_buy
                                else self.state.position, Decimal(0))
                filled = min(filled, reducible)
            self.state.position += filled if intent.is_buy else -filled
            self.state.accepted += 1
            # IOC remainder is canceled, while cumulative fill is retained.
            status = OrderState.FILLED if filled == intent.qty else OrderState.CANCELED
            report = OrderReport(intent.client_id, status, filled, True)
        self.state.intents[intent.client_id] = intent
        self.state.reports[intent.client_id] = report
        if report.filled:
            self.state.events.append((intent.client_id + ":fill:1", report))
        if self.fault == "lose_ack":
            raise TimeoutError("accepted, response lost")
        return report

    async def query(self, client_id):
        if self.query_lag:
            self.query_lag -= 1
            return OrderReport(client_id, OrderState.UNKNOWN, reason="not indexed yet")
        return self.state.reports.get(client_id) or OrderReport(
            client_id, OrderState.UNKNOWN, reason="not found; finality unproven")

    async def cancel(self, client_id):
        self.cancel_calls += 1
        report = self.state.reports.get(client_id)
        if report and report.state in (OrderState.OPEN, OrderState.PARTIAL):
            self.state.reports[client_id] = OrderReport(client_id, OrderState.CANCELED,
                                                      report.filled, True)
        if self.fault == 'lose_cancel_ack':
            raise TimeoutError('cancel accepted, response lost')

    def repeated_events(self):
        return self.state.events + self.state.events[::-1] + self.state.events

    async def fetch_snapshot(self):
        import time
        from entropy_arb.account_state import AccountSnapshot
        orders = tuple(k for k,r in self.state.reports.items() if r.state in (OrderState.OPEN,OrderState.PARTIAL))
        return AccountSnapshot(self.venue,self.account,self.state.position,Decimal(1000),time.time(),True,orders,
                               settled_through=time.time(),sequence=self.state.accepted)

    async def query_fills(self, client_id):
        from entropy_arb.account_state import TradeFill
        intent=self.state.intents.get(client_id)
        fee_bps=Decimal(".9") if self.venue=="entropy" else Decimal(0)
        return [TradeFill(client_id,key,report.filled,intent.limit_px,report.filled*intent.limit_px*fee_bps/10000)
                for key,report in self.state.events if report.client_id==client_id]
