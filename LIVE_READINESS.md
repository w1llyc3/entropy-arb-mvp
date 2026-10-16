# P5 execution readiness — 2026-10-16

**Release decision: automatic live execution remains NOT READY. Default remains RECORD_ONLY.**

The follow-up branch `codex/live-adapter-review` adds isolated, opt-in manual
SNDK contract-test adapters/commands. See `MANUAL_LIVE_TEST.md`. This is a code
candidate for user review, not proof of real account validation. The user will
populate env and run manual tests; Codex has not accessed those credentials or
sent real orders. The historical P5-preparation results below describe the
preceding increment and must not be read as authentication/settlement evidence.

This branch is a P5 preparation increment, not completion of P5. It does not
load private keys, install signing SDKs, enable real submission/cancellation,
adopt external positions, merge the PR stack, or restart the running collector.

## Confirmed by Code

### Durable cancellation contract

`ExecutionVenue.cancel(client_id)` requests cancellation; its response is not
an order outcome. The new `POST /api/orders/cancel` accepts `confirm=true`,
`leg_id`, and a stable `idempotency_key`, through the same serialized service
worker as manual/automatic execution. It is simulation-only. The existing
`POST /api/confirm/cancel` still cancels only a pending local proposal.

Cancellation states are PREPARED → SUBMITTING → ACKNOWLEDGED or UNKNOWN →
VERIFIED. The cancel intent and send boundary are committed before transport.
The same request never sends twice; reusing its key for another leg fails.
A different key cannot bypass an unresolved cancellation for the same leg.

After acknowledgement, timeout, or connection failure, query the original
client ID. Only authoritative terminal order evidence can mark the cancellation
attempt VERIFIED. This includes an order that filled during cancellation;
VERIFIED means the final order state was queried, not necessarily canceled.
It does not prove the account reconciled. Fill history and settlement/position
checks must still pass before reservations are released or opening resumes.

Recovery is query-only. Even a crash after the cancel send boundary but before
transport cannot silently resend the cancel. An unresolved attempt remains
blocked for operator investigation; automatic cancel retry policy is not added.

SQLite v3 adds only the cancellation table/index. Existing v1/v2 ledgers receive
a unique adjacent `.pre-v3-<uuid>.sqlite` backup using SQLite's backup API before
the v3 migration (including committed WAL content). Existing order/client IDs
are preserved. Unknown/incomplete schemas remain rejected. These backups may
contain public account and trade history; do not commit them. Rollback after a
new cancellation must not restore an older DB and lose its evidence.

### Continuity and trading sessions

Before queued commands or strategy ticks, detect >15-second scheduler gaps,
backward clocks, or >2-second wall/monotonic clock divergence. Invalidate
cached books, cancel stale proposals, reset signal persistence and recovery,
and record `last_continuity_fault`. Fresh books and account reconciliation are
required again. `status.continuity_fault` exposes the last event; a successful
heartbeat alone does not validate quotes. This detects long stalls too; it is
not a claim that Windows power events were exercised.

The new service uses the same verified 2026–2028 cash-equity reference calendar
as chart labels for its regular-session check. Holidays, early closes and
unverified years no longer qualify for automatic regular-session execution.
This is a strategy reference window, not exchange/perpetual market hours.
Legacy test-only/archived routes retain their old calendar behavior. Manual
non-RTH acknowledgement semantics remain unchanged.

## External documentation checked, not runtime validation

- Hyperliquid documents a 128-bit hexadecimal client ID, IOC cancellation of
  the unfilled remainder, and cancellation by client ID:
  https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/exchange-endpoint
- Lighter documents authenticated inactive-order history with a cursor and
  limit 1–100:
  https://apidocs.lighter.xyz/reference/accountinactiveorders
- Official Lighter SDK source to pin and verify before adapter integration:
  https://github.com/elliottech/lighter-python/blob/main/lighter/signer_client.py

Documentation is not proof of account-specific permissions, client-ID
deduplication, history completeness, fill ordering, or settlement finality.
No new live adapter is connected to the persistent dispatcher in this branch.

## Required before a small SNDK real-order test

1. Pin the official SDK revisions and implement stable external ID mapping
   (including Lighter integer limits) without replacing IDs after timeouts.
2. Capture sanitized real read responses for both venues: order lookup,
   active/inactive pagination, fills/fees, positions, sequence/finality.
   Verify completeness and identify a defensible settlement boundary. The
   current Lighter public reader intentionally remains incomplete.
3. Implement real submit/query/cancel adapters and test accepted-but-ack-lost,
   cancel/fill races, duplicate requests, process crashes and reconnection
   using those verified fixtures. An acknowledgement must never unlock risk.
4. Validate tick/size/minimum notional, fees, margin and net-exposure tolerance
   against SNDK's current market/account rules. Existing fixed assumptions are
   not considered verified production limits.
5. On this Windows device, run a disposable fake service with no credentials;
   observe real sleep/resume and disconnect/reconnect. Verify old proposals
   fail, cached quotes are invalidated, no extra submits occur, and fresh
   evidence is required. Do not suspend a machine holding live exposure.
6. Review at least 24 hours of fake execution-service soak: heartbeat gaps,
   unknown ages, cancellation backlog, reconciliation failures, DB health,
   order counts, duplicate sends and shutdown/restart evidence. Existing
   45-hour market CSV coverage is not execution-service soak evidence.
7. Complete backup/migration rehearsal and review the stacked PRs. Agree a
   small test notional/order count/account scope and manual stop policy before
   enabling the real adapter. No automatic external-position flattening.

## Suspected Risk / Requires Runtime Validation

- Windows sleep behavior and external clock changes are simulated by injected
  clocks only; real device results are still required.
- `samples=61` remains a known recording/validation discrepancy. This branch
  does not rewrite history or claim its timer cause is established.
- Fixed budget/collateral/net-exposure parameters and abbreviated exception
  diagnostics need follow-up. They were not silently re-tuned here.
- A long stall inside a live SDK call must still be classified by the real
  adapter. Clearing cached quotes cannot cancel an exchange-accepted order.

## Validation

New tests cover cancellation acknowledgement without finality, lost response,
late fills, delayed indexing, failed DB commits, concurrent claim, restart
without resend, same-key conflict, service reconciliation, record-only API
denial, v2 backup/migration, simulated clock gaps and verified session edges.
Final regression results are recorded in the PR description. No real orders
or real Windows sleep tests were executed.

Final local verification: Python **303 passed, 1 skipped** (POSIX-only lock),
one existing Starlette/httpx deprecation warning, 21.80 seconds. Chart JavaScript
**7 passed**. Three cancel crash phases use child-process `os._exit(73)` and
independent fake-exchange evidence, not graceful exception handling alone.

```powershell
.\.venv-audit\Scripts\python.exe -m pytest tests -q --basetemp=.web/p5-final --tb=short
node --test tests/chart_time.test.cjs
```
