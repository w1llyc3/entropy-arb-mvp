"""Isolated observation sessions. No account clients, keys, orders or recorder controls.

This is the first workbench milestone, not a live execution owner. SQLite is
authoritative for session rules/lifecycle only, never exchange inventory.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .watchlist import asset_for


class SessionConflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def rules_for(body):
    if not isinstance(body, dict):
        raise ValueError("rules must be an object")
    allowed = {"symbol", "strategy", "direction", "entry_bps", "exit_bps", "order_usd",
               "max_inventory_usd", "max_unhedged_usd", "duration_minutes"}
    if set(body) - allowed:
        raise ValueError("unknown rule fields")
    rules = dict(symbol="SNDK", strategy="SPREAD", direction="BUY_ENTROPY", entry_bps=10,
                 exit_bps=2, order_usd=11, max_inventory_usd=22,
                 max_unhedged_usd=11, duration_minutes=60)
    rules.update(body)
    asset_for(rules["symbol"])
    if rules["strategy"] != "SPREAD":
        raise SessionConflict("passive quoting is a later milestone")
    if rules["direction"] not in ("BUY_ENTROPY", "SELL_ENTROPY"):
        raise ValueError("invalid direction")
    for field in allowed - {"symbol", "strategy", "direction"}:
        value = rules[field]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("finite numeric rules required")
    if not 0 <= rules["exit_bps"] < rules["entry_bps"] <= 10000:
        raise ValueError("require 0 <= exit < entry <= 10000 bps")
    if not 0 < rules["order_usd"] <= rules["max_inventory_usd"] <= 25:
        raise ValueError("require 0 < order <= inventory <= $25 per leg")
    if not 0 < rules["max_unhedged_usd"] <= rules["order_usd"]:
        raise ValueError("invalid unhedged limit")
    if not 1 <= rules["duration_minutes"] <= 1440:
        raise ValueError("duration must be 1–1440 minutes")
    return rules


class SessionStore:
    def __init__(self, path, *, now=time.time):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.now = now
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise SessionConflict("unsupported workbench schema")
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == 0 and tables:
            self.db.close()
            raise SessionConflict("existing unversioned database requires explicit migration")
        if version == 1 and not {"sessions", "commands", "events", "reports"} <= tables:
            self.db.close()
            raise SessionConflict("incomplete workbench schema")
        self.db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS sessions(
                id TEXT PRIMARY KEY, rules TEXT NOT NULL, state TEXT NOT NULL,
                version INTEGER NOT NULL, created REAL NOT NULL, started REAL, ended REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_session ON sessions((1))
                WHERE state != 'ENDED';
            CREATE TABLE IF NOT EXISTS commands(
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL,
                kind TEXT NOT NULL, observed REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reports(
                session_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            PRAGMA user_version=1;
            COMMIT;
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def event(self, sid, kind, payload=None):
        self.db.execute("INSERT INTO events(session_id,kind,observed,payload) VALUES(?,?,?,?)",
                        (sid, kind, self.now(), encode(payload or {})))

    def recover(self):
        with self.transaction():
            rows = self.db.execute("SELECT id FROM sessions WHERE state='OBSERVING'").fetchall()
            for row in rows:
                self.db.execute("UPDATE sessions SET state='RECOVERY_REQUIRED',version=version+1 WHERE id=?",
                                (row["id"],))
                self.event(row["id"], "PROCESS_RESTART_OBSERVATION_INTERRUPTED")

    def _session(self, sid):
        row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError(sid)
        result = dict(row)
        result["rules"] = json.loads(result["rules"])
        # Existing v1 rules predate the watchlist and were exclusively SNDK.
        # Display normalization only: preserve stored rules/events/reports.
        result["rules"].setdefault("symbol", "SNDK")
        result.update(mode="OBSERVATION_ONLY", live_enabled=False, rules_version=1)
        return result

    def expire(self):
        with self.transaction():
            rows = self.db.execute("SELECT * FROM sessions WHERE state='OBSERVING'").fetchall()
            for row in rows:
                if self.now() >= row["started"] + json.loads(row["rules"])["duration_minutes"] * 60:
                    self.db.execute("UPDATE sessions SET state='PAUSED',version=version+1 WHERE id=?", (row["id"],))
                    self.event(row["id"], "DURATION_REACHED_OBSERVATION_PAUSED")

    def command(self, action, key, *, sid=None, version=None, rules=None):
        if not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise ValueError("idempotency key required (8–128 characters)")
        fingerprint = hashlib.sha256(encode(dict(action=action, sid=sid, version=version, rules=rules)).encode()).hexdigest()
        with self.transaction():
            cached = self.db.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
            if cached:
                if cached["fingerprint"] != fingerprint:
                    raise SessionConflict("idempotency key reused with different command")
                return json.loads(cached["result"])
            if action == "create":
                normalized = rules_for({} if rules is None else rules)
                if self.db.execute("SELECT 1 FROM sessions WHERE state!='ENDED'").fetchone():
                    raise SessionConflict("end the existing observation session first")
                sid = uuid.uuid4().hex
                self.db.execute("INSERT INTO sessions VALUES(?,?,?,1,?,NULL,NULL)",
                                (sid, encode(normalized), "DRAFT", self.now()))
                self.event(sid, "RULES_SAVED", normalized)
            else:
                current = self._session(sid)
                if type(version) is not int or version != current["version"]:
                    raise SessionConflict("session changed; refresh before retrying")
                state = current["state"]
                transitions = {"start": ({"DRAFT"}, "OBSERVING"),
                               "pause": ({"OBSERVING"}, "PAUSED"),
                               "resume": ({"PAUSED", "RECOVERY_REQUIRED"}, "OBSERVING"),
                               "end": ({"DRAFT", "OBSERVING", "PAUSED", "RECOVERY_REQUIRED"}, "ENDED")}
                if action not in transitions or state not in transitions[action][0]:
                    raise SessionConflict("invalid session transition")
                if action == "resume" and current["started"] is not None:
                    if self.now() >= current["started"] + current["rules"]["duration_minutes"] * 60:
                        raise SessionConflict("session duration exhausted; end and create a new session")
                self.db.execute("UPDATE sessions SET state=?,version=version+1,started=CASE WHEN ?='start' THEN ? ELSE started END,ended=CASE WHEN ?='end' THEN ? ELSE ended END WHERE id=?",
                                (transitions[action][1], action, self.now(), action, self.now(), sid))
                self.event(sid, "OBSERVATION_" + action.upper())
                if action == "end":
                    self.db.execute("INSERT INTO reports VALUES(?,?)", (sid, encode(self._report(sid))))
            result = self._session(sid)
            self.db.execute("INSERT INTO commands VALUES(?,?,?)", (key, fingerprint, encode(result)))
            return result

    def events(self, sid):
        return [dict(seq=r["seq"], kind=r["kind"], observed=r["observed"], payload=json.loads(r["payload"]))
                for r in self.db.execute("SELECT * FROM events WHERE session_id=? ORDER BY seq", (sid,))]

    def _report(self, sid):
        return dict(session=self._session(sid), generated_at=self.now(), status="PRELIMINARY_OBSERVATION",
                    exchange_positions_verified=False, settlement_verified=False,
                    actual_inventory=None, realized_pnl_usd=None, fees_usd=None, funding_usd=None,
                    own_fill_distribution=None, events=self.events(sid),
                    notice="Observation lifecycle only. No execution integration, account reconciliation or inferred zero balances.")

    def report(self, sid):
        with self.lock:
            self._session(sid)
            row = self.db.execute("SELECT payload FROM reports WHERE session_id=?", (sid,)).fetchone()
            return json.loads(row["payload"]) if row else self._report(sid)

    def snapshot(self):
        with self.lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM sessions ORDER BY created DESC,rowid DESC LIMIT 100")]
            sessions = [self._session(sid) for sid in ids]
            active = next((s for s in sessions if s["state"] != "ENDED"), None)
            return dict(mode="OBSERVATION_ONLY", live_enabled=False, sessions=sessions, active=active,
                        inventory=None, account_verified=False, execution_connected=False,
                        own_fills=None, realized_pnl_usd=None,
                        events=self.events(active["id"]) if active else [])

    def close(self):
        self.db.close()
