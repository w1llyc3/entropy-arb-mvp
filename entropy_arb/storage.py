"""SQLite execution ledger. No credentials, network, or position inference.

Terminal leg outcomes still require position reconciliation (P3). This
ledger deliberately has no method to turn local fills into trading readiness.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import sqlite3
import threading
import time
import uuid

from .execution_contract import OrderIntent, OrderReport, OrderState


class IdempotencyConflict(ValueError): pass
class RecoveryRequired(RuntimeError): pass
class EvidenceConflict(RuntimeError): pass


@dataclass(frozen=True)
class LegSpec:
    venue: str
    account: str  # public identifier/alias, never signer material
    qty: Decimal
    limit_px: Decimal
    is_buy: bool
    reduce_only: bool = False

    def payload(self):
        if self.venue not in ("entropy", "lighter") or not self.account.strip():
            raise ValueError("SNDK venue and public account required")
        OrderIntent("validate", self.qty, self.limit_px, self.is_buy, self.reduce_only)
        if type(self.is_buy) is not bool or type(self.reduce_only) is not bool:
            raise ValueError("boolean sides required")
        return dict(venue=self.venue, account=self.account,
                    qty=str(self.qty.normalize()), limit_px=str(self.limit_px.normalize()),
                    is_buy=self.is_buy, reduce_only=self.reduce_only)


TERMINAL = {s.value for s in (OrderState.FILLED, OrderState.CANCELED,
                             OrderState.REJECTED, OrderState.NOT_SENT)}


class Ledger:
    def __init__(self, path, *, timeout=0.5):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=timeout, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise RecoveryRequired("unsupported ledger schema")
            if version == 0:
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
                    raise RecoveryRequired("unversioned database requires explicit migration")
                self.db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE operations(
                        id TEXT PRIMARY KEY, proposal TEXT UNIQUE NOT NULL,
                        payload_hash TEXT NOT NULL, intent TEXT NOT NULL,
                        state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                    CREATE TABLE commands(
                        key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                        operation_id TEXT NOT NULL REFERENCES operations(id));
                    CREATE TABLE legs(
                        id TEXT PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
                        venue TEXT NOT NULL, account TEXT NOT NULL, market TEXT NOT NULL,
                        qty TEXT NOT NULL, limit_px TEXT NOT NULL, is_buy INTEGER NOT NULL,
                        reduce_only INTEGER NOT NULL, client_id TEXT NOT NULL,
                        submit_state TEXT NOT NULL, outcome TEXT NOT NULL,
                        filled TEXT NOT NULL DEFAULT '0', reservation TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(venue,account,client_id), UNIQUE(operation_id,venue));
                    CREATE TABLE fills(
                        venue TEXT NOT NULL, account TEXT NOT NULL, trade_id TEXT NOT NULL,
                        leg_id TEXT NOT NULL REFERENCES legs(id), qty TEXT NOT NULL,
                        price TEXT NOT NULL, fee TEXT NOT NULL,
                        PRIMARY KEY(venue,account,trade_id));
                    CREATE TABLE events(
                        id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES operations(id),
                        leg_id TEXT REFERENCES legs(id), kind TEXT NOT NULL, created REAL NOT NULL);
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            required = {"operations", "commands", "legs", "fills", "events"}
            present = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not required <= present:
                raise RecoveryRequired("incomplete ledger schema")
            if version in (0, 1):
                self.db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE snapshots(id INTEGER PRIMARY KEY,venue TEXT NOT NULL,
                        account TEXT NOT NULL,observed REAL NOT NULL,payload TEXT NOT NULL);
                    CREATE TABLE baselines(operation_id TEXT NOT NULL REFERENCES operations(id),
                        venue TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(operation_id,venue));
                    CREATE TABLE proposals(id TEXT PRIMARY KEY,payload TEXT NOT NULL,
                        state TEXT NOT NULL,operation_id TEXT REFERENCES operations(id));
                    CREATE TABLE reconcile_jobs(id TEXT PRIMARY KEY,created REAL NOT NULL,
                        status TEXT NOT NULL,result TEXT);
                    PRAGMA user_version=2;
                    COMMIT;
                """)
            extra = {"settings", "snapshots", "baselines", "proposals", "reconcile_jobs"}
            present = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not extra <= present:
                raise RecoveryRequired("incomplete service schema")
            if version < 3:
                # Preserve the complete previous ledger, including WAL contents.
                # Existing databases only; a new empty ledger needs no backup.
                if version:
                    backup = sqlite3.connect(str(path) + '.pre-v3-' + uuid.uuid4().hex + '.sqlite')
                    try:
                        self.db.backup(backup)
                    finally:
                        backup.close()
                self.db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE cancellations(
                        key TEXT PRIMARY KEY, leg_id TEXT NOT NULL REFERENCES legs(id),
                        state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
                    CREATE UNIQUE INDEX one_pending_cancel_per_leg ON cancellations(leg_id)
                        WHERE state != 'VERIFIED';
                    PRAGMA user_version=3;
                    COMMIT;
                """)
            if 'cancellations' not in {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                raise RecoveryRequired("incomplete cancellation schema")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RecoveryRequired("ledger integrity failure")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        with self.lock: self.db.close()

    def abort_plan(self,operation_id):
        """Release only a transactionally proven unsent plan; never an order."""
        with self.transaction() as db:
            op=db.execute('SELECT state FROM operations WHERE id=?',(operation_id,)).fetchone()
            if not op: raise KeyError(operation_id)
            if op['state']=='ABORTED': return False
            legs=list(db.execute('SELECT * FROM legs WHERE operation_id=?',(operation_id,)))
            if (op['state']!='INTENT_COMMITTED' or not legs or any(l['submit_state']!='PREPARED' or Decimal(l['filled'])!=0 for l in legs)
                or db.execute('SELECT 1 FROM fills f JOIN legs l ON f.leg_id=l.id WHERE l.operation_id=?',(operation_id,)).fetchone()):
                raise RecoveryRequired('plan crossed send boundary; query orders instead')
            db.execute("UPDATE legs SET submit_state='REPORTED',outcome='not_sent',reservation='0',version=version+1 WHERE operation_id=?",(operation_id,))
            db.execute("UPDATE operations SET state='ABORTED',updated=? WHERE id=?",(time.time(),operation_id))
            self._event(db,operation_id,None,'PLAN_ABORTED_PROVEN_UNSUBMITTED')
        return True

    def prepare_cancel(self, key, leg_id):
        if not isinstance(key, str) or not key.strip():
            raise ValueError("stable cancellation key required")
        with self.transaction() as db:
            prior = db.execute("SELECT * FROM cancellations WHERE key=?", (key,)).fetchone()
            if prior:
                if prior['leg_id'] != leg_id:
                    raise IdempotencyConflict("cancellation key belongs to another leg")
                return dict(prior)
            leg = db.execute("SELECT * FROM legs WHERE id=?", (leg_id,)).fetchone()
            if not leg: raise KeyError(leg_id)
            if leg['submit_state'] == 'PREPARED':
                raise RecoveryRequired("order has not crossed the send boundary")
            if leg['outcome'] in TERMINAL:
                raise RecoveryRequired("order already terminal; reconcile instead")
            if db.execute("SELECT 1 FROM cancellations WHERE leg_id=? AND state!='VERIFIED'", (leg_id,)).fetchone():
                raise RecoveryRequired("cancellation pending; query before another attempt")
            now = time.time()
            db.execute("INSERT INTO cancellations VALUES(?,?,?,?,?)", (key, leg_id, 'PREPARED', now, now))
            self._event(db, leg['operation_id'], leg_id, 'CANCEL_INTENT_COMMITTED')
        return self.cancellation(key)

    def cancellation(self, key):
        with self.lock:
            row = self.db.execute("SELECT * FROM cancellations WHERE key=?", (key,)).fetchone()
            if not row: raise KeyError(key)
            return dict(row)

    def claim_cancel(self, key):
        with self.transaction() as db:
            changed = db.execute("UPDATE cancellations SET state='SUBMITTING',updated=? WHERE key=? AND state='PREPARED'",
                                 (time.time(), key)).rowcount
            if changed:
                leg = db.execute("SELECT l.* FROM legs l JOIN cancellations c ON c.leg_id=l.id WHERE c.key=?", (key,)).fetchone()
                self._event(db, leg['operation_id'], leg['id'], 'CANCEL_SEND_BOUNDARY_COMMITTED')
            return bool(changed)

    def cancel_ack(self, key, *, acknowledged):
        # Never change the order outcome or release its risk reservation here.
        with self.transaction() as db:
            db.execute("UPDATE cancellations SET state=?,updated=? WHERE key=? AND state='SUBMITTING'",
                       ('ACKNOWLEDGED' if acknowledged else 'UNKNOWN', time.time(), key))

    def verify_cancellations(self, leg_id):
        """Only authoritative final order evidence resolves a cancellation attempt."""
        with self.transaction() as db:
            leg = db.execute("SELECT * FROM legs WHERE id=?", (leg_id,)).fetchone()
            if not leg or leg['outcome'] not in TERMINAL: return
            changed = db.execute("UPDATE cancellations SET state='VERIFIED',updated=? WHERE leg_id=? AND state!='VERIFIED'",
                                 (time.time(), leg_id)).rowcount
            if changed: self._event(db, leg['operation_id'], leg_id, 'CANCEL_ORDER_FINAL_VERIFIED')

    @contextmanager
    def transaction(self, *, immediate=True):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction: self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def _event(db, op, leg, kind):
        db.execute("INSERT INTO events(operation_id,leg_id,kind,created) VALUES(?,?,?,?)",
                   (op, leg, kind, time.time()))

    def create_intent(self, key, proposal, intent, legs, *, baselines=None):
        if not key or not proposal or intent not in ("open", "close", "reduce"):
            raise ValueError("command/proposal/intent required")
        specs = sorted([leg.payload() for leg in legs], key=lambda p: p["venue"])
        if not specs or len(specs) > 2 or len({p["venue"] for p in specs}) != len(specs):
            raise ValueError("one leg per venue required")
        if intent in ("open", "close") and len(specs) != 2:
            raise ValueError("paired operation needs both legs")
        if intent == "open":
            if any(p["reduce_only"] for p in specs) or specs[0]["is_buy"] == specs[1]["is_buy"]:
                raise ValueError("open must have opposing, non-reducing legs")
            if Decimal(specs[0]["qty"]) != Decimal(specs[1]["qty"]):
                raise ValueError("open quantities must match")
        elif not all(p["reduce_only"] for p in specs):
            raise ValueError("close/reduce must be reduce-only")
        digest = hashlib.sha256(json.dumps(dict(proposal=proposal, intent=intent, legs=specs),
                                           sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            prior = db.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
            if prior:
                if prior["payload_hash"] != digest: raise IdempotencyConflict("key payload changed")
                return prior["operation_id"]
            prior = db.execute("SELECT * FROM operations WHERE proposal=?", (proposal,)).fetchone()
            if prior:
                if prior["payload_hash"] != digest: raise IdempotencyConflict("proposal payload changed")
                db.execute("INSERT INTO commands VALUES(?,?,?)", (key, digest, prior["id"]))
                return prior["id"]
            # No release on local terminal outcomes: P3 must query positions first.
            active = list(db.execute("SELECT id,state FROM operations WHERE state NOT IN ('SETTLED','ABORTED')"))
            reduction = (intent == "reduce" and active
                         and all(r["state"] == "NEEDS_REDUCTION" for r in active))
            if active and not reduction:
                raise RecoveryRequired("position reconciliation required before another operation")
            op = uuid.uuid4().hex
            now = time.time()
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                       (op, proposal, digest, intent, "INTENT_COMMITTED", now, now))
            for p in specs:
                leg_id, client_id = uuid.uuid4().hex, uuid.uuid4().hex
                db.execute("""INSERT INTO legs(id,operation_id,venue,account,market,qty,
                    limit_px,is_buy,reduce_only,client_id,submit_state,outcome,reservation)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (leg_id, op, p["venue"], p["account"], "SNDK", p["qty"], p["limit_px"],
                     int(p["is_buy"]), int(p["reduce_only"]), client_id, "PREPARED", "unknown",
                     str(Decimal(p["qty"]) * Decimal(p["limit_px"]))))
            db.execute("INSERT INTO commands VALUES(?,?,?)", (key, digest, op))
            if baselines is not None:
                for p in specs:
                    snapshot = baselines[p["venue"]]
                    if snapshot.account != p["account"]:
                        raise ValueError("baseline account mismatch")
                    db.execute("INSERT INTO baselines VALUES(?,?,?)", (op,p["venue"],json.dumps(snapshot.payload())))
            db.execute("UPDATE proposals SET state='CONSUMED',operation_id=? WHERE id=?", (op,proposal))
            self._event(db, op, None, "INTENT_COMMITTED")
            return op

    def operation(self, op):
        # One SQLite snapshot even if another process commits between SELECTs.
        with self.transaction(immediate=False) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (op,)).fetchone()
            if not row: raise KeyError(op)
            result = dict(row)
            result["legs"] = [dict(r) for r in db.execute(
                "SELECT * FROM legs WHERE operation_id=? ORDER BY venue", (op,))]
            return result

    def claim(self, op):
        """Both legs cross the durable send boundary in one short transaction."""
        with self.transaction() as db:
            row = db.execute("SELECT state FROM operations WHERE id=?", (op,)).fetchone()
            if not row: raise KeyError(op)
            if row[0] != "INTENT_COMMITTED": return False
            db.execute("UPDATE legs SET submit_state='SUBMITTING',version=version+1 WHERE operation_id=?", (op,))
            db.execute("UPDATE operations SET state='EXECUTING',updated=? WHERE id=?", (time.time(), op))
            self._event(db, op, None, "SEND_BOUNDARY_COMMITTED")
            return True

    def save_report(self, leg_id, report: OrderReport):
        conflict = False
        with self.transaction() as db:
            row = db.execute("SELECT * FROM legs WHERE id=?", (leg_id,)).fetchone()
            if not row: raise KeyError(leg_id)
            if row["submit_state"] == "PREPARED":
                raise EvidenceConflict("report before send boundary")
            if report.client_id != row["client_id"] or report.filled > Decimal(row["qty"]):
                conflict = True
            elif report.state == OrderState.UNKNOWN:
                if row["outcome"] in TERMINAL:
                    return False  # A failed query is not contrary terminal evidence.
            elif report.filled < Decimal(row["filled"]):
                return False  # stale cumulative observation; never roll fill backward
            elif row["outcome"] in TERMINAL:
                if report.state.value == row["outcome"] and report.filled == Decimal(row["filled"]):
                    return False
                conflict = True  # terminal evidence challenged, not overwritten silently
            elif report.state == OrderState.FILLED and report.filled != Decimal(row["qty"]):
                conflict = True
            filled = max(Decimal(row["filled"]), report.filled) if not conflict else Decimal(row["filled"])
            outcome = "unknown" if conflict else report.state.value
            submit = "UNKNOWN" if outcome == "unknown" else "REPORTED"
            db.execute("UPDATE legs SET submit_state=?,outcome=?,filled=?,version=version+1 WHERE id=?",
                       (submit, outcome, str(filled), leg_id))
            self._event(db, row["operation_id"], leg_id, "EVIDENCE_CONFLICT" if conflict else "REPORT_" + outcome)
            self._aggregate(db, row["operation_id"])
        if conflict: raise EvidenceConflict("inconsistent order evidence; query required")
        return True

    def _aggregate(self, db, op):
        rows = list(db.execute("SELECT * FROM legs WHERE operation_id=?", (op,)))
        if any(r["outcome"] == "unknown" for r in rows): state = "RECOVERING"
        elif any(r["outcome"] not in TERMINAL for r in rows): state = "EXECUTING"
        else:
            net = sum((Decimal(r["filled"]) * (1 if r["is_buy"] else -1) for r in rows), Decimal(0))
            intent = db.execute("SELECT intent FROM operations WHERE id=?", (op,)).fetchone()[0]
            # Reductions need an account baseline; their signed fill delta is
            # NOT the remaining net position (one-sided flatten is legitimate).
            state = "NEEDS_REDUCTION" if intent == "open" and net else "RECONCILING"
        db.execute("UPDATE operations SET state=?,updated=? WHERE id=?", (state, time.time(), op))

    def record_fill(self, leg_id, trade_id, qty, price, fee=Decimal(0)):
        try:
            return self._record_fill(leg_id, trade_id, qty, price, fee)
        except EvidenceConflict:
            # Preserve the rejected observation as a durable recovery reason.
            # Do not log its payload, and do not mutate confirmed fill records.
            with self.transaction() as db:
                row = db.execute("SELECT operation_id FROM legs WHERE id=?", (leg_id,)).fetchone()
                if row:
                    db.execute("UPDATE legs SET submit_state='UNKNOWN',outcome='unknown',version=version+1 WHERE id=?", (leg_id,))
                    self._event(db, row[0], leg_id, "FILL_EVIDENCE_CONFLICT")
                    self._aggregate(db, row[0])
            raise

    def _record_fill(self, leg_id, trade_id, qty, price, fee):
        if not trade_id or any(not v.is_finite() for v in (qty, price, fee)) or qty <= 0 or price <= 0:
            raise ValueError("finite fill fields required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM legs WHERE id=?", (leg_id,)).fetchone()
            if not row: raise KeyError(leg_id)
            if row["submit_state"] == "PREPARED":
                raise EvidenceConflict("fill before send boundary")
            prior = db.execute("SELECT * FROM fills WHERE venue=? AND account=? AND trade_id=?",
                               (row["venue"], row["account"], trade_id)).fetchone()
            if prior:
                if prior["leg_id"] != leg_id or any(Decimal(prior[k]) != v for k,v in
                                                    (("qty",qty),("price",price),("fee",fee))):
                    raise EvidenceConflict("duplicate trade ID with changed evidence")
                return False
            total = sum((Decimal(r[0]) for r in db.execute("SELECT qty FROM fills WHERE leg_id=?", (leg_id,))), Decimal(0))
            if total + qty > Decimal(row["qty"]):
                raise EvidenceConflict("fill total exceeds intended quantity")
            db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)",
                       (row["venue"], row["account"], trade_id, leg_id, str(qty), str(price), str(fee)))
            self._event(db, row["operation_id"], leg_id, "FILL_RECORDED")
            return True

    def pending_operations(self):
        with self.lock:
            return [r[0] for r in self.db.execute("SELECT id FROM operations WHERE state NOT IN ('SETTLED','ABORTED') ORDER BY created")]

    def metrics(self):
        with self.transaction(immediate=False):
            oldest = self.db.execute("""SELECT min(o.created) FROM operations o JOIN legs l
                ON o.id=l.operation_id WHERE l.submit_state IN ('SUBMITTING','UNKNOWN')""").fetchone()[0]
            return dict(trading_ready=False, mode="RECORD_ONLY", operations=self.db.execute(
                "SELECT count(*) FROM operations").fetchone()[0], unknown_legs=self.db.execute(
                "SELECT count(*) FROM legs WHERE submit_state IN ('SUBMITTING','UNKNOWN')").fetchone()[0],
                fills=self.db.execute("SELECT count(*) FROM fills").fetchone()[0],
                pending_cancellations=self.db.execute("SELECT count(*) FROM cancellations WHERE state!='VERIFIED'").fetchone()[0],
                events=self.db.execute("SELECT count(*) FROM events").fetchone()[0],
                oldest_unknown_age_sec=max(0, time.time()-oldest) if oldest is not None else None)

    def abort_unsubmitted(self, op):
        with self.transaction() as db:
            row = db.execute("SELECT state FROM operations WHERE id=?", (op,)).fetchone()
            if not row or row[0] != "INTENT_COMMITTED": return False
            db.execute("UPDATE legs SET submit_state='REPORTED',outcome='not_sent',version=version+1 WHERE operation_id=?", (op,))
            db.execute("UPDATE operations SET state='RECONCILING',updated=? WHERE id=?", (time.time(), op))
            self._event(db, op, None, "RECOVERY_ABORT_PROVEN_UNSUBMITTED")
            return True

    def get_setting(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key,json.dumps(value)))

    def proposal(self, proposal_id=None):
        with self.lock:
            if proposal_id:
                row = self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            else:
                row = self.db.execute("SELECT * FROM proposals WHERE state='PENDING' ORDER BY rowid DESC LIMIT 1").fetchone()
            return (dict(json.loads(row["payload"]), status=row["state"], operation_id=row["operation_id"])
                    if row else None)

    def put_proposal(self, payload):
        with self.transaction() as db:
            db.execute("UPDATE proposals SET state='EXPIRED' WHERE state='PENDING'")
            db.execute("INSERT INTO proposals(id,payload,state) VALUES(?,?,'PENDING')",
                       (payload["proposal_id"],json.dumps(payload)))

    def cancel_proposal(self):
        with self.transaction() as db:
            db.execute("UPDATE proposals SET state='CANCELED' WHERE state='PENDING'")

    def save_snapshots(self, snapshots):
        with self.transaction() as db:
            for snapshot in snapshots.values():
                db.execute("INSERT INTO snapshots(venue,account,observed,payload) VALUES(?,?,?,?)",
                           (snapshot.venue,snapshot.account,snapshot.observed_at,json.dumps(snapshot.payload())))

    def latest_snapshot(self, venue, account):
        with self.lock:
            row = self.db.execute("SELECT payload FROM snapshots WHERE venue=? AND account=? ORDER BY id DESC LIMIT 1",
                                  (venue,account)).fetchone()
            return json.loads(row[0]) if row else None

    def all_operations(self):
        with self.lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM operations ORDER BY created")]
        return [self.operation(op) for op in ids]

    def baseline_count(self, op):
        with self.lock:
            return self.db.execute("SELECT count(*) FROM baselines WHERE operation_id=?", (op,)).fetchone()[0]

    def fill_total(self, leg_id):
        with self.lock:
            return sum((Decimal(r[0]) for r in self.db.execute("SELECT qty FROM fills WHERE leg_id=?", (leg_id,))),Decimal(0))

    def start_reconcile(self):
        job = uuid.uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO reconcile_jobs(id,created,status) VALUES(?,?,'RUNNING')", (job,time.time()))
        return job

    def finish_reconcile(self, job, result):
        with self.transaction() as db:
            db.execute("UPDATE reconcile_jobs SET status=?,result=? WHERE id=?",
                       ("COMPLETE" if result["ok"] else "BLOCKED",json.dumps(result),job))

    def settle_verified(self, versions):
        """Recovery coordinator supplies evidence; CAS rejects interleaved changes."""
        with self.transaction() as db:
            for leg_id, version in versions.items():
                row = db.execute("SELECT version,outcome FROM legs WHERE id=?", (leg_id,)).fetchone()
                if not row or row["version"] != version or row["outcome"] not in TERMINAL:
                    raise EvidenceConflict("order changed during position reconciliation")
            rows = list(db.execute("SELECT id FROM operations WHERE state NOT IN ('SETTLED','ABORTED')"))
            for row in rows:
                actual = {r[0] for r in db.execute("SELECT id FROM legs WHERE operation_id=?", (row[0],))}
                if not actual <= versions.keys(): raise EvidenceConflict("missing reconciliation legs")
                db.execute("UPDATE operations SET state='SETTLED',updated=? WHERE id=?", (time.time(),row[0]))
                db.execute("UPDATE legs SET reservation='0' WHERE operation_id=?", (row[0],))
                self._event(db,row[0],None,"POSITIONS_AND_FILLS_RECONCILED")
