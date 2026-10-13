"""P1 transport-independent contract; not a durable execution service.

UNKNOWN preserves any known cumulative fill. A submission exception does
not prove rejection. P2 uses fake-only dispatch; production adapter migration is P3.
"""
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol


class OrderState(str, Enum):
    UNKNOWN = "unknown"
    OPEN = "open"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    NOT_SENT = "not_sent"


@dataclass(frozen=True)
class OrderIntent:
    client_id: str
    qty: Decimal
    limit_px: Decimal
    is_buy: bool
    reduce_only: bool = False

    def __post_init__(self):
        if not self.client_id.strip():
            raise ValueError("stable client ID required")
        for value in (self.qty, self.limit_px):
            if not value.is_finite() or value <= 0:
                raise ValueError("positive finite quantity and price required")


@dataclass(frozen=True)
class OrderReport:
    client_id: str
    state: OrderState
    filled: Decimal = Decimal(0)
    authoritative: bool = False
    reason: str = ""

    def __post_init__(self):
        if not self.client_id or not self.filled.is_finite() or self.filled < 0:
            raise ValueError("invalid report")
        if self.state in (OrderState.REJECTED, OrderState.NOT_SENT) and self.filled:
            raise ValueError("rejected/not-sent cannot carry fills")
        if self.state != OrderState.UNKNOWN and not self.authoritative:
            raise ValueError("known outcome requires evidence")


class BeforeSubmitError(Exception):
    """Caller can prove transport submission never began."""


def submission_exception(client_id: str, exc: Exception) -> OrderReport:
    if isinstance(exc, BeforeSubmitError):
        return OrderReport(client_id, OrderState.NOT_SENT, authoritative=True,
                           reason="local validation failed")
    # Do not expose exception payloads (SDK errors may contain credentials).
    return OrderReport(client_id, OrderState.UNKNOWN, reason=type(exc).__name__)


class ExecutionVenue(Protocol):
    async def submit(self, intent: OrderIntent) -> OrderReport: ...
    async def query(self, client_id: str) -> OrderReport: ...
