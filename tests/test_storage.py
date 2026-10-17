from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
import sqlite3

import pytest

from entropy_arb.storage import Ledger, LegSpec, IdempotencyConflict, RecoveryRequired, EvidenceConflict
from entropy_arb.execution_contract import OrderReport, OrderState


def legs(q=".01"):
    return [LegSpec("entropy", "fake-account-e", D(q), D("1600"), False),
            LegSpec("lighter", "fake-account-l", D(q), D("1599"), True)]


@pytest.fixture
def ledger(tmp_path):
    store = Ledger(tmp_path / "ledger.sqlite")
    yield store
    store.close()


def test_intent_ids_reservations_and_constraints_survive_reopen(tmp_path):
    path = tmp_path / "ledger.sqlite"
    a = Ledger(path)
    op = a.create_intent("key", "proposal", "open", legs())
    before = a.operation(op)
    assert len(before["legs"]) == 2
    assert all(l["submit_state"] == "PREPARED" and D(l["reservation"]) > 0 for l in before["legs"])
    assert a.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert a.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert a.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    a.close()
    b = Ledger(path)
    assert b.operation(op) == before
    assert b.create_intent("key", "proposal", "open", legs()) == op
    b.close()


def test_abort_and_send_claim_are_mutually_exclusive(ledger):
    op=ledger.create_intent('key','proposal','open',legs())
    assert ledger.abort_plan(op)
    assert not ledger.claim(op)
    assert all(D(l['reservation'])==0 and l['outcome']=='not_sent' for l in ledger.operation(op)['legs'])
    sent=ledger.create_intent('new-key','new-proposal','open',legs())
    assert ledger.claim(sent)
    with pytest.raises(RecoveryRequired): ledger.abort_plan(sent)
    assert all(D(l['reservation'])>0 for l in ledger.operation(sent)['legs'])


def test_concurrent_command_and_proposal_consumption(tmp_path):
    path = tmp_path / "ledger.sqlite"
    stores = [Ledger(path), Ledger(path)]
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            ids = list(pool.map(lambda i: stores[i % 2].create_intent(
                "same-key", "same-proposal", "open", legs()), range(100)))
        assert len(set(ids)) == 1
        with ThreadPoolExecutor(max_workers=12) as pool:
            aliases = list(pool.map(lambda i: stores[i % 2].create_intent(
                f"alias-{i}", "same-proposal", "open", legs()), range(50)))
        assert set(aliases) == set(ids)
        assert stores[0].metrics()["operations"] == 1
        assert sum(store.claim(ids[0]) for store in stores) == 1
    finally:
        for store in stores: store.close()


def test_payload_conflict_and_position_gate(ledger):
    op = ledger.create_intent("k", "p", "open", legs())
    with pytest.raises(IdempotencyConflict): ledger.create_intent("k", "p", "open", legs(".02"))
    with pytest.raises(IdempotencyConflict): ledger.create_intent("other", "p", "open", legs(".02"))
    with pytest.raises(RecoveryRequired): ledger.create_intent("next", "next", "open", legs())
    ledger.claim(op)
    for l in ledger.operation(op)["legs"]:
        ledger.save_report(l["id"], OrderReport(l["client_id"], OrderState.FILLED, D(l["qty"]), True))
    assert ledger.operation(op)["state"] == "RECONCILING"
    assert not ledger.metrics()["trading_ready"]
    with pytest.raises(RecoveryRequired): ledger.create_intent("next", "next", "open", legs())


def test_unique_fills_and_monotonic_reports(ledger):
    op = ledger.create_intent("k", "p", "open", legs())
    ledger.claim(op)
    l = ledger.operation(op)["legs"][0]
    ledger.save_report(l["id"], OrderReport(l["client_id"], OrderState.PARTIAL, D(".004"), True))
    assert not ledger.save_report(l["id"], OrderReport(l["client_id"], OrderState.OPEN, D(0), True))
    for _ in range(10): ledger.record_fill(l["id"], "fake-trade-1", D(".004"), D(1600))
    assert ledger.metrics()["fills"] == 1
    with pytest.raises(EvidenceConflict): ledger.record_fill(l["id"], "fake-trade-1", D(".005"), D(1600))
    assert ledger.operation(op)["state"] == "RECOVERING"
    with pytest.raises(EvidenceConflict): ledger.record_fill(l["id"], "too-much", D(".01"), D(1600))
    assert D(ledger.operation(op)["legs"][0]["filled"]) == D(".004")
    assert ledger.metrics()["oldest_unknown_age_sec"] is not None


def test_unknown_keeps_known_partial_fill_and_uncertainty(ledger):
    op = ledger.create_intent("k", "p", "open", legs())
    ledger.claim(op)
    l = ledger.operation(op)["legs"][0]
    ledger.save_report(l["id"], OrderReport(l["client_id"], OrderState.PARTIAL, D(".004"), True))
    ledger.save_report(l["id"], OrderReport(l["client_id"], OrderState.UNKNOWN))
    current = ledger.operation(op)["legs"][0]
    assert current["outcome"] == "unknown"
    assert D(current["filled"]) == D(".004")


def test_contradictory_terminal_report_closes_gate(ledger):
    op = ledger.create_intent("k", "p", "open", legs())
    ledger.claim(op)
    l = ledger.operation(op)["legs"][0]
    ledger.save_report(l["id"], OrderReport(l["client_id"], OrderState.CANCELED, D(".004"), True))
    with pytest.raises(EvidenceConflict):
        ledger.save_report(l["id"], OrderReport(l["client_id"], OrderState.FILLED, D(".01"), True))
    assert ledger.operation(op)["state"] == "RECOVERING"
    assert D(ledger.operation(op)["legs"][0]["filled"]) == D(".004")


def test_transaction_failure_is_atomic(ledger):
    def fail(*a): raise sqlite3.OperationalError("simulated disk full")
    ledger._event = fail
    with pytest.raises(sqlite3.OperationalError): ledger.create_intent("k", "p", "open", legs())
    assert ledger.metrics()["operations"] == 0
    assert ledger.db.execute("SELECT count(*) FROM legs").fetchone()[0] == 0


def test_busy_database_is_bounded_and_does_not_commit(tmp_path):
    path = tmp_path / "ledger.sqlite"
    store = Ledger(path, timeout=.01)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError): store.create_intent("k", "p", "open", legs())
    finally:
        blocker.execute("ROLLBACK"); blocker.close()
    assert store.metrics()["operations"] == 0
    store.close()


def test_corrupt_database_not_replaced(tmp_path):
    path = tmp_path / "bad.sqlite"
    data = b"this is intentionally not sqlite"
    path.write_bytes(data)
    with pytest.raises(sqlite3.DatabaseError): Ledger(path)
    assert path.read_bytes() == data


@pytest.mark.parametrize("version", [0, 1, 2])
def test_unrecognized_or_incomplete_schema_not_silently_migrated(tmp_path, version):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE legacy(value TEXT)")
    conn.execute("INSERT INTO legacy VALUES('retain this evidence')")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit(); conn.close()
    with pytest.raises(RecoveryRequired): Ledger(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT value FROM legacy").fetchone()[0] == "retain this evidence"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='operations'").fetchone() is None
    conn.close()
