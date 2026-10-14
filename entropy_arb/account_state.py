"""Read evidence, not inferred local positions."""
from dataclasses import dataclass
from decimal import Decimal
import math


@dataclass(frozen=True)
class AccountSnapshot:
    venue: str
    account: str
    position: Decimal | None
    available: Decimal | None
    observed_at: float
    complete: bool
    open_orders: tuple = ()
    settled_through: float | None = None
    sequence: int | None = None

    def valid(self, now, max_age=10):
        return (self.complete is True and math.isfinite(self.observed_at)
                and 0 <= now - self.observed_at <= max_age
                and (self.settled_through is None or
                     (math.isfinite(self.settled_through) and 0<=self.settled_through<=now))
                and (self.sequence is None or (type(self.sequence) is int and self.sequence>=0))
                and self.position is not None and self.position.is_finite()
                and self.available is not None and self.available.is_finite()
                and self.available >= 0)

    def payload(self):
        return dict(venue=self.venue, account=self.account,
                    position=str(self.position) if self.position is not None else None,
                    available=str(self.available) if self.available is not None else None,
                    observed_at=self.observed_at, complete=self.complete,
                    open_orders=list(self.open_orders), settled_through=self.settled_through,
                    sequence=self.sequence)


@dataclass(frozen=True)
class TradeFill:
    client_id: str
    trade_id: str
    qty: Decimal
    price: Decimal
    fee: Decimal
