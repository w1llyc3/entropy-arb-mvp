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
    def __init__(self, state=None, *, fault=None, fill_fraction=Decimal(1),
                 query_lag=0):
        self.state = state if state is not None else ExchangeState()
        self.fault = fault
        self.fill_fraction = fill_fraction
        if not 0 <= fill_fraction <= 1:
            raise ValueError("invalid fraction")
        self.query_lag = query_lag
        self.submit_calls = 0

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

    def repeated_events(self):
        return self.state.events + self.state.events[::-1] + self.state.events

