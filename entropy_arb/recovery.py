"""Query-only reconciliation. Never submits, retries an order, or adopts external positions."""
import asyncio
from decimal import Decimal
import time

from .execution_contract import OrderReport, OrderState
from .storage import EvidenceConflict, TERMINAL


class RecoveryCoordinator:
    def __init__(self, ledger, venues, accounts, *, now=time.time, timeout=5):
        self.ledger, self.venues, self.accounts = ledger, venues, accounts
        self.now, self.timeout = now, timeout
        self.snapshots = {}

    async def run(self, *, full=False):
        job = self.ledger.start_reconcile()
        errors, queries = [], 0
        ids=([op["id"] for op in self.ledger.all_operations()] if full else self.ledger.pending_operations())
        for op_id in ids:
            if self.ledger.abort_unsubmitted(op_id): continue
            with self.ledger.lock:
                unsent=self.ledger.db.execute("SELECT 1 FROM events WHERE operation_id=? AND kind='RECOVERY_ABORT_PROVEN_UNSUBMITTED'",(op_id,)).fetchone()
            # A durably proven pre-send abort has no exchange order to query.
            # Querying its never-used IDs would turn NOT_SENT back into Unknown.
            if unsent: continue
            for leg in self.ledger.operation(op_id)["legs"]:
                venue = self.venues.get(leg["venue"])
                if venue is None or self.accounts.get(leg["venue"]) != leg["account"]:
                    errors.append("account or venue mismatch"); continue
                try:
                    queries += 1
                    report = await asyncio.wait_for(venue.query(leg["client_id"]),self.timeout)
                    if report.state.value not in TERMINAL: errors.append("order query not final")
                except Exception:
                    report = OrderReport(leg["client_id"],OrderState.UNKNOWN)
                    errors.append("order query unavailable")
                try:
                    self.ledger.save_report(leg["id"],report)
                    self.ledger.verify_cancellations(leg["id"])
                    queries += 1
                    fills = await asyncio.wait_for(venue.query_fills(leg["client_id"]),self.timeout)
                    for fill in fills:
                        if fill.client_id != leg["client_id"]: raise EvidenceConflict("fill identity mismatch")
                        self.ledger.record_fill(leg["id"],fill.trade_id,fill.qty,fill.price,fill.fee)
                except Exception:
                    errors.append("order or fill evidence incomplete")

        fresh,observed = {},{}
        for name, venue in self.venues.items():
            try:
                queries += 1
                snapshot = await asyncio.wait_for(venue.fetch_snapshot(),self.timeout)
                if snapshot.venue != name or snapshot.account != self.accounts.get(name):
                    raise EvidenceConflict("snapshot identity mismatch")
                from dataclasses import replace
                observed[name]=replace(snapshot,complete=False)
                old = self.ledger.latest_snapshot(name,snapshot.account)
                if old:
                    if snapshot.observed_at<old["observed_at"]:
                        raise EvidenceConflict("regressed account observation")
                    if old["sequence"] is not None and (snapshot.sequence is None or snapshot.sequence < old["sequence"]):
                        raise EvidenceConflict("regressed account sequence")
                if not snapshot.valid(self.now()): raise EvidenceConflict("incomplete or stale account")
                fresh[name] = snapshot
                observed[name]=snapshot
            except Exception:
                errors.append(name + " account unavailable or stale")
        self.snapshots = observed
        if set(fresh) != {"entropy","lighter"}: errors.append("both complete accounts required")
        if fresh:
            self.ledger.save_snapshots(fresh)
            if max(s.observed_at for s in fresh.values())-min(s.observed_at for s in fresh.values()) > 2:
                errors.append("account observation skew")
        operations = self.ledger.all_operations()
        if any(s.open_orders for s in fresh.values()): errors.append("outstanding orders require resolution")
        baseline = self.ledger.get_setting("managed_baseline")
        if baseline is None and not operations and not errors:
            if any(s.position != 0 for s in fresh.values()):
                errors.append("external positions: explicit adoption required")
            else:
                baseline = {n:dict(account=s.account,position="0") for n,s in fresh.items()}
                self.ledger.set_setting("managed_baseline",baseline)
        if baseline is None: errors.append("unverified legacy or external baseline")
        expected = {n:Decimal((baseline or {}).get(n,{}).get("position","0")) for n in self.accounts}
        versions = {}
        for op in operations:
            if self.ledger.baseline_count(op["id"]) != len(op["legs"]):
                errors.append("operation missing verified baseline")
            for leg in op["legs"]:
                if leg["account"] != self.accounts.get(leg["venue"]):
                    errors.append("ledger account differs from configured account"); continue
                if leg["outcome"] not in TERMINAL: errors.append("nonterminal or unknown order")
                quantity = Decimal(leg["filled"])
                if self.ledger.fill_total(leg["id"]) != quantity:
                    errors.append("fill history differs from cumulative order report")
                expected[leg["venue"]] += quantity * (1 if leg["is_buy"] else -1)
                if op["state"] not in ("SETTLED","ABORTED"): versions[leg["id"]] = leg["version"]
        if baseline:
            for name,snapshot in fresh.items():
                if baseline.get(name,{}).get("account") != snapshot.account:
                    errors.append("baseline account mismatch")
                if snapshot.position != expected.get(name): errors.append(name + " unexplained position delta")
        if operations:
            with self.ledger.lock:
                boundary = self.ledger.db.execute("SELECT max(created) FROM events WHERE kind='SEND_BOUNDARY_COMMITTED'").fetchone()[0]
            if boundary and any(s.settled_through is None or s.settled_through < boundary for s in fresh.values()):
                errors.append("position settlement watermark unavailable")
        net = sum((s.position for s in fresh.values()),Decimal(0)) if len(fresh)==2 else None
        if net is not None and abs(net) > Decimal("0.001"):
            errors.append("confirmed residual net exposure; manual reduction required")
        if not errors:
            self.ledger.settle_verified(versions)
        result = dict(job_id=job,ok=not errors,routed=queries>0,query_count=queries,
                      reason="; ".join(dict.fromkeys(errors)) or "orders, fills and positions agree",
                      net_base=str(net) if net is not None else None,ts=self.now())
        self.ledger.finish_reconcile(job,result)
        self.ledger.set_setting("last_reconcile",result)
        return result
