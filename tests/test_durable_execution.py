import asyncio
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from entropy_arb.execution import OfflineExecutor
from entropy_arb.execution_contract import BeforeSubmitError, OrderReport, OrderState
from entropy_arb.storage import Ledger, RecoveryRequired
from fakes.venues import FakeVenue
from test_storage import legs


@pytest.fixture
def context(tmp_path):
    store = Ledger(tmp_path / "ledger.sqlite")
    op = store.create_intent("key", "proposal", "open", legs())
    venues = {"entropy": FakeVenue(), "lighter": FakeVenue()}
    yield store, op, venues
    store.close()


def test_default_dispatcher_never_submits_and_rejects_live_adapters(context):
    store, op, venues = context
    assert asyncio.run(OfflineExecutor(store, venues).execute(op))["state"] == "INTENT_COMMITTED"
    assert sum(v.submit_calls for v in venues.values()) == 0
    with pytest.raises(ValueError): OfflineExecutor(store, {"entropy": object()})


def test_concurrent_execute_does_not_send_twice(context):
    store, op, venues = context
    executor = OfflineExecutor(store, venues, allow_submit=True)
    async def run():
        await asyncio.gather(*(executor.execute(op) for _ in range(30)))
    asyncio.run(run())
    assert [v.submit_calls for v in venues.values()] == [1, 1]
    assert store.operation(op)["state"] == "RECONCILING"
    assert not store.metrics()["trading_ready"]


def test_submit_entry_observes_committed_intent_and_ids(context):
    store, op, venues = context
    def observe(stage, value):
        if stage == "before_submit":
            other = Ledger(store.db.execute("PRAGMA database_list").fetchone()[2])
            try:
                record = other.operation(op)
                assert all(l["submit_state"] in ("SUBMITTING", "REPORTED") for l in record["legs"])
                assert value["client_id"] in {l["client_id"] for l in record["legs"]}
            finally: other.close()
    asyncio.run(OfflineExecutor(store, venues, allow_submit=True, hook=observe).execute(op))


def test_claim_failure_no_network_submission(context):
    store, op, venues = context
    def fail(*a): raise sqlite3.OperationalError("disk full")
    store._event = fail
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(OfflineExecutor(store, venues, allow_submit=True).execute(op))
    assert sum(v.submit_calls for v in venues.values()) == 0
    assert store.operation(op)["state"] == "INTENT_COMMITTED"


def test_accepted_but_ack_lost_is_recovered_only_by_query(context):
    store, op, venues = context
    for v in venues.values(): v.fault = "lose_ack"
    executor = OfflineExecutor(store, venues, allow_submit=True)
    assert asyncio.run(executor.execute(op))["state"] == "RECOVERING"
    ids = [l["client_id"] for l in store.operation(op)["legs"]]
    assert store.metrics()["unknown_legs"] == 2
    for v in venues.values(): v.query_lag = 1
    assert asyncio.run(executor.recover())[0]["state"] == "RECOVERING"
    assert asyncio.run(executor.recover())[0]["state"] == "RECONCILING"
    asyncio.run(executor.execute(op))
    assert [v.submit_calls for v in venues.values()] == [1, 1]
    assert ids == [l["client_id"] for l in store.operation(op)["legs"]]
    assert not store.metrics()["trading_ready"]


@pytest.mark.parametrize("error", [ConnectionError, BeforeSubmitError])
def test_query_failure_keeps_partial_fill_without_resubmission(context, error):
    store, op, venues = context
    store.claim(op)
    leg = store.operation(op)["legs"][0]
    store.save_report(leg["id"], OrderReport(leg["client_id"], OrderState.PARTIAL, D(".004"), True))
    async def down(client_id): raise error("fake outage")
    for v in venues.values(): v.query = down
    result = asyncio.run(OfflineExecutor(store, venues, allow_submit=True).recover())[0]
    assert result["state"] == "RECOVERING"
    assert D(result["legs"][0]["filled"]) == D(".004")
    assert sum(v.submit_calls for v in venues.values()) == 0


@pytest.mark.parametrize("fill_fraction,expected", [("0.4", "NEEDS_REDUCTION"), ("1", "RECONCILING")])
def test_partial_results_keep_reservations_and_never_auto_hedge(context, fill_fraction, expected):
    store, op, venues = context
    venues["entropy"].fill_fraction = D(fill_fraction)
    result = asyncio.run(OfflineExecutor(store, venues, allow_submit=True).execute(op))
    assert result["state"] == expected
    assert sum(v.submit_calls for v in venues.values()) == 2
    assert all(D(l["reservation"]) > 0 for l in result["legs"])
    with pytest.raises(RecoveryRequired): store.create_intent("next", "next", "open", legs())


def test_equal_partial_close_remains_reconciling_with_remaining_quantity(tmp_path):
    store = Ledger(tmp_path / "ledger.sqlite")
    from entropy_arb.storage import LegSpec
    specs = [LegSpec("entropy", "fake-e", D(".01"), D(1600), True, True),
             LegSpec("lighter", "fake-l", D(".01"), D(1600), False, True)]
    op = store.create_intent("close", "close", "close", specs)
    venues = {"entropy": FakeVenue(fill_fraction=D(".4")), "lighter": FakeVenue(fill_fraction=D(".4"))}
    venues["entropy"].state.position = D("-.01")
    venues["lighter"].state.position = D(".01")
    result = asyncio.run(OfflineExecutor(store, venues, allow_submit=True).execute(op))
    assert result["state"] == "RECONCILING"
    assert all(D(l["qty"]) - D(l["filled"]) == D(".006") for l in result["legs"])
    assert venues["entropy"].state.position == D("-.006")
    assert not store.metrics()["trading_ready"]
    store.close()


def test_result_commit_failure_leaves_recoverable_send_boundary(context):
    store, op, venues = context
    original = store._event
    def fail_report(db, operation, leg, kind):
        if kind.startswith("REPORT_"): raise sqlite3.OperationalError("simulated result disk full")
        original(db, operation, leg, kind)
    store._event = fail_report
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(OfflineExecutor(store, venues, allow_submit=True).execute(op))
    assert store.metrics()["unknown_legs"] == 2
    assert sum(v.state.accepted for v in venues.values()) == 2
    store._event = original
    executor = OfflineExecutor(store, venues, allow_submit=True)
    assert asyncio.run(executor.recover())[0]["state"] == "RECONCILING"
    assert sum(v.submit_calls for v in venues.values()) == 2


def test_one_sided_reduce_does_not_invent_remaining_net(tmp_path):
    from entropy_arb.storage import LegSpec
    store = Ledger(tmp_path / "ledger.sqlite")
    op = store.create_intent("reduce", "reduce", "reduce", [
        LegSpec("entropy", "fake-e", D(".01"), D(1600), True, True)])
    v = FakeVenue()
    v.state.position = D("-.01")
    result = asyncio.run(OfflineExecutor(store, {"entropy": v}, allow_submit=True).execute(op))
    assert result["state"] == "RECONCILING"
    assert v.state.position == 0
    assert not store.metrics()["trading_ready"]
    store.close()


def test_cross_process_commands_share_one_operation(tmp_path):
    path = tmp_path / "ledger.sqlite"
    store = Ledger(path)
    store.close()
    code = r'''
from decimal import Decimal as D
import sys
from entropy_arb.storage import Ledger,LegSpec
s=Ledger(sys.argv[1],timeout=3)
legs=[LegSpec('entropy','fake-e',D('.01'),D(1600),False),
      LegSpec('lighter','fake-l',D('.01'),D(1599),True)]
print(s.create_intent('same','proposal','open',legs))
s.close()
'''
    children = [subprocess.Popen([sys.executable, "-c", code, str(path)],
                  cwd=Path(__file__).parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for _ in range(4)]
    ids = []
    try:
        for child in children:
            out, err = child.communicate(timeout=15)
            assert child.returncode == 0, err
            ids.append(out.strip())
    finally:
        for child in children:
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)
    assert len(set(ids)) == 1


@pytest.mark.parametrize("phase", ["inside_claim_transaction", "claimed", "one_accepted", "both_accepted", "one_report_committed"])
def test_process_crash_reopens_without_resubmission(tmp_path, phase):
    path, server_path = tmp_path / "ledger.sqlite", tmp_path / "fake-server.json"
    store = Ledger(path)
    op = store.create_intent("key", "proposal", "open", legs())
    ids = [l["client_id"] for l in store.operation(op)["legs"]]
    store.close()
    # Independent fake server evidence is retained outside the crashed client.
    code = r'''
import json,os,sys
from decimal import Decimal
from entropy_arb.storage import Ledger
from entropy_arb.execution_contract import OrderReport,OrderState
path,op,server,phase=sys.argv[1:]
store=Ledger(path)
if phase=='inside_claim_transaction':
    store._event=lambda *args: os._exit(73)
store.claim(op)
known={}
if phase not in ('inside_claim_transaction','claimed'):
    for i,leg in enumerate(store.operation(op)['legs']):
        known[leg['client_id']]=leg['qty']
        if phase=='one_accepted': break
with open(server,'w') as f:
    json.dump(known,f);f.flush();os.fsync(f.fileno())
if phase=='one_report_committed':
    leg=store.operation(op)['legs'][0]
    store.save_report(leg['id'],OrderReport(leg['client_id'],OrderState.FILLED,Decimal(leg['qty']),True))
os._exit(73)
'''
    proc = subprocess.run([sys.executable, "-c", code, str(path), op, str(server_path), phase],
                          cwd=Path(__file__).parents[1], capture_output=True, timeout=15)
    assert proc.returncode == 73, proc.stderr
    reopened = Ledger(path)
    known = json.loads(server_path.read_text()) if server_path.exists() else {}
    class QueryOnly:
        is_simulation = True
        async def submit(self, intent): pytest.fail("recovery must never submit")
        async def query(self, client_id):
            return (OrderReport(client_id, OrderState.FILLED, D(known[client_id]), True)
                    if client_id in known else OrderReport(client_id, OrderState.UNKNOWN))
    result = asyncio.run(OfflineExecutor(reopened, {"entropy": QueryOnly(), "lighter": QueryOnly()},
                                          allow_submit=True).recover())[0]
    assert ids == [l["client_id"] for l in result["legs"]]
    assert result["state"] == ("RECONCILING" if phase in
        ("inside_claim_transaction", "both_accepted", "one_report_committed") else "RECOVERING")
    assert not reopened.metrics()["trading_ready"]
    assert reopened.create_intent("key", "proposal", "open", legs()) == op
    reopened.close()
