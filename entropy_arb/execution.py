"""Shared durable transport mechanics; default production remains RECORD_ONLY.

OfflineExecutor accepts simulation venues only. The isolated operator test
uses ManualLiveExecutor with independent review/size/account gates. Recovery
here queries orders only; no local report unlocks automatic trading.
"""
import asyncio
from decimal import Decimal

from .execution_contract import OrderIntent, OrderReport, OrderState, submission_exception


class DurableDispatcher:
    def __init__(self, ledger, venues, *, allow_submit=False, hook=None):
        self.ledger, self.venues = ledger, venues
        self.allow_submit = allow_submit
        self.hook = hook or (lambda stage, value: None)

    @staticmethod
    def intent(leg):
        return OrderIntent(leg["client_id"], Decimal(leg["qty"]), Decimal(leg["limit_px"]),
                           bool(leg["is_buy"]), bool(leg["reduce_only"]))

    async def execute(self, operation_id):
        op = self.ledger.operation(operation_id)
        if not self.allow_submit:
            return op  # Default RECORD_ONLY, including synthetic transport.
        if any(leg["venue"] not in self.venues for leg in op["legs"]):
            raise ValueError("missing execution venue")
        if not self.ledger.claim(operation_id):
            return self.ledger.operation(operation_id)  # Never resend a claimed operation.
        self.hook("after_claim", operation_id)

        async def send(leg):
            self.hook("before_submit", leg)
            try:
                report = await self.venues[leg["venue"]].submit(self.intent(leg))
            except Exception as exc:
                report = submission_exception(leg["client_id"], exc)
            self.hook("before_report_commit", leg)
            # A DB failure propagates; durable SUBMITTING remains recoverable.
            self.ledger.save_report(leg["id"], report)
            self.hook("after_report_commit", leg)

        results = await asyncio.gather(*(send(leg) for leg in op["legs"]), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException): raise result
        return self.ledger.operation(operation_id)

    async def recover(self):
        """Never submits. PREPARED is proven unsent; all other uncertainty is queried."""
        results = []
        for op_id in self.ledger.pending_operations():
            if self.ledger.abort_unsubmitted(op_id):
                results.append(self.ledger.operation(op_id))
                continue
            op = self.ledger.operation(op_id)
            for leg in op["legs"]:
                try:
                    report = await self.venues[leg["venue"]].query(leg["client_id"])
                except Exception as exc:
                    # Even a local query validation failure says nothing
                    # about whether an earlier submission was accepted.
                    report = OrderReport(leg["client_id"], OrderState.UNKNOWN,
                                         reason=type(exc).__name__)
                self.ledger.save_report(leg["id"], report)
                self.ledger.verify_cancellations(leg["id"])
            results.append(self.ledger.operation(op_id))
        return results

    async def cancel(self, key, leg_id, *, timeout=5):
        """Durable single-attempt cancel; acknowledgement never settles."""
        if not self.allow_submit:
            raise ValueError("RECORD_ONLY: cancellation disabled")
        self.ledger.prepare_cancel(key, leg_id)
        if not self.ledger.claim_cancel(key):
            return self.ledger.cancellation(key)
        with self.ledger.lock:
            leg = dict(self.ledger.db.execute("SELECT * FROM legs WHERE id=?", (leg_id,)).fetchone())
        self.hook("before_cancel", leg)
        try:
            await asyncio.wait_for(self.venues[leg['venue']].cancel(leg['client_id']), timeout)
        except Exception:
            self.ledger.cancel_ack(key, acknowledged=False)
        else:
            self.ledger.cancel_ack(key, acknowledged=True)
        self.hook("after_cancel_ack", leg)
        # A lost cancel response may race a fill. Query, never infer zero fills.
        try:
            report = await asyncio.wait_for(self.venues[leg['venue']].query(leg['client_id']), timeout)
        except Exception:
            report = OrderReport(leg['client_id'], OrderState.UNKNOWN)
        self.ledger.save_report(leg_id, report)
        self.ledger.verify_cancellations(leg_id)
        return self.ledger.cancellation(key)


class OfflineExecutor(DurableDispatcher):
    def __init__(self,ledger,venues,*,allow_submit=False,hook=None):
        if not venues or any(getattr(v,'is_simulation',False) is not True for v in venues.values()):
            raise ValueError('offline simulation venues required; no live adapters')
        super().__init__(ledger,venues,allow_submit=allow_submit,hook=hook)
