# Independent multi-asset Session workbench

## 2026-10-17: watchlist and read-only position milestone

The workbench now switches between SNDK, ANTH, DRAM, OAI, NBIS, EWY, XIAOMI,
IONQ, GPRO and TCNT using the existing fixed watchlist and exact venue aliases.
New observation Sessions freeze `rules.symbol`; existing rules without that field
display as SNDK without rewriting old events/reports. One open Session remains
the limit. Browsing another asset does not retarget an existing Session, and
its action buttons are disabled until switching back. History/report views are
asset-filtered. Stale chart/report responses are discarded after switching.

Paired history shows both USD mid-price lines and signed basis shading on the
right bps axis. Single-venue history shows only the Entropy USD line and never
creates fake paired basis. Missing history stays empty. Data gaps remain visible.

Optional public position monitor:

```powershell
& ./.venv-audit/Scripts/python.exe -m web.workbench --port 8770 --public-env-file ../.env
```

Only `HL_ACCOUNT_ADDRESS` / `ENTROPY_HL_ACCOUNT` and `LIGHTER_ACCOUNT_INDEX`
are selected for parsing. Private keys/tokens are ignored, no SDK is loaded,
and account identifiers do not appear in the browser API payload. Configuration
is explicit and not discovered automatically. No account locks are acquired.
Without the argument, the workbench remains credential-free and makes no account
requests. Fixed official public endpoints are queried every 15 seconds, shared
across the watchlist; GET polling only reads cached projections.

Positions are account-wide observations, not Session-attributed inventory.
Missing/ambiguous markets, account failures, nonfinite quantities and snapshots
older than 30 seconds remain UNKNOWN. Lighter public positions do not establish
outstanding-order completeness. Both legs' quantities are shown separately;
contract equivalence is unverified, net USD exposure remains null, and no
settlement or execution readiness is granted. Session reports still do not claim
verified inventory/PnL. Account balance fields are account-wide, not per-asset.

The prior milestone notes below describe the original implementation. Account
client absence applies to default startup; the optional monitor is new. Real
execution stays disabled for every watchlist asset, including SNDK. Adapter and
contract validation must be completed per asset before adding execution support.

Validation for this milestone: **412 Python tests passed, 1 skipped** with the
existing deprecation warning; **4 offline Node UI tests passed** for all choices,
out-of-order chart responses, single-venue rendering, Session action isolation
and delayed report downloads. JS syntax and whitespace checks passed. These are
script/DOM harness tests, not an actual browser rendering pass.

Live local HTTP checks returned paired history and fresh public position reads
for SNDK, ANTH, DRAM, OAI, NBIS, EWY, XIAOMI and TCNT. IONQ/GPRO returned
single-venue history with unknown Lighter positions, as intended. All assets
remained execution_ready=false. Collector PID 26504 remained RECORD_ONLY.
The in-app browser control connection timed out during this milestone, so visual
and manual selector acceptance remains unverified; do not reuse the previous
milestone's browser pass as evidence for the new selector/chart layout.

## Agreed product scope

Use two local sites: existing record-only collection/research on 8766, a new
execution workbench on 8770. Build spread-triggered arbitrage first, then passive
quoting with cross-venue hedging. One strategy per Session initially; both must
eventually share one authoritative account inventory and execution risk budget.

The intended loop is rules → Session → inventory/execution monitoring → report.
SNDK execution only. Additional research assets remain record-only.

## Current milestone: observation lifecycle, not live execution

Run from the repository root:

```powershell
& ./.venv-audit/Scripts/python.exe -m web.workbench --port 8770
```

Open http://127.0.0.1:8770/. This command reads no env, creates no exchange
clients, does not acquire account execution locks, and never constructs the
existing collector/TradingService. It uses `.web/session-workbench/sessions.sqlite`.
The existing manual live ledger and collector ledger remain separate.

`--data-root` explicitly selects the recorder repository for read-only historical
CSV access; by default it is this repository. `--state-dir` selects the workbench's
own lifecycle storage. No arbitrary directory selection is offered in the browser.

Implemented:

- Frozen SNDK spread rules: direction, entry/exit thresholds, per-leg order and
  inventory limits, unhedged limit, total Session duration including pauses.
- Durable DRAFT → OBSERVING → PAUSED → OBSERVING → ENDED lifecycle. Restarting an
  observing Session enters RECOVERY_REQUIRED and requires explicit continuation.
- An expired Session pauses; resuming does not reset its duration. Timer and
  command handling enforce the boundary. A draft can be ended without starting.
- SQLite transactions, idempotency keys, optimistic versions, one open Session,
  process ownership, persistent lifecycle events, immutable end report.
- Read-only SNDK history chart, gap breaks, Beijing / US Eastern display.
- End report JSON download containing rules, timestamps and lifecycle events.

The page states OBSERVATION_ONLY, execution disconnected, inventory unknown.
Reports remain PRELIMINARY_OBSERVATION; inventory, fills, fees, funding and PnL
are null. A finished observation is not proof of flat positions or settlement.
There is no send/cancel/flatten endpoint, account read integration, synthetic
fill generator, passive quoting or automatic signal evaluation in this milestone.
The chart is historical middle-price context, never an executable trading signal.

Rules values are configuration only and do not activate risk enforcement on a
real account. The $25 ceiling is deliberately bounded for the later reviewed
manual validation; it is not a production sizing recommendation.

## Next implementation milestones

1. **Execution ownership separation.** Remove account execution ownership from
   the future collector-only service while retaining the single recorder owner.
   Add a persistent execution owner with per-account locks across processes and
   workspaces. Verify old collectors do not hold those locks before starting it;
   never delete locks to bypass a conflict. This migration may require one planned
   collector restart, but recurring trading Sessions must not restart collection.
2. **Read-only inventory integration.** Attach exchange snapshots to Session
   projections: quantities, pending orders, balances, observation timestamps,
   freshness, recovery status. Unknown remains unknown. Define account baseline,
   external/manual trades, and ownership of inventory carried across Sessions.
3. **Spread strategy in simulation.** Use fresh depth and executable bid/ask for
   each direction. Define round-trip cost, fee/funding treatment, exit economics,
   quantity matching, slippage limits, maximum open inventory/unhedged duration
   and loss limits before enabling triggers. Freeze and hash the rule version.
   Simulate partial fills, cancel/fill races, one-leg failures and restart recovery.
4. **Reviewed bounded live execution.** Reuse durable intents, stable client IDs,
   adapter checks, unknown-state recovery and account reconciliation. Opening the
   site or pressing observation start must never arm real execution. Live requires
   separate explicit review/arming. Existing manual CLI gates remain intact.
5. **Inventory management and reporting.** Persist Session ↔ operation/fill links.
   Show gross per-leg inventory and net delta separately, unhedged duration,
   realized/unrealized PnL, fee/funding status, executable spreads, slippage and
   hedge latency. Persist actual-fill price/edge distributions, never infer fills
   from quotes. End stops new entries; existing orders and inventory continue to
   be managed. Require confirmed disposition of remaining inventory before a
   final report, with preliminary reports until exchange evidence is complete.
6. **Passive strategy.** Post-only on one designated venue, hedge confirmed fills
   on the other; handle queue uncertainty, stale quotes, quote cancellation,
   cancel/fill races, inventory skew and adverse selection. Keep one strategy per
   Session until shared-budget interaction is tested.

Existing 24-hour fake execution soak and physical Windows sleep/resume validation
remain separate gates. The new page does not claim they passed or enable trading.

## Verification

`tests/test_workbench.py` covers duplicate concurrent commands, conflicting keys,
restart with preserved rules, stale browser commands, expiry/resume budget,
immutable preliminary reports, invalid numeric/configuration fields, isolation
from collector controls/credentials, cross-origin writes and duplicate process
ownership. Run the full suite before review. Runtime browser verification covers
the initial page, historical chart and explicit unknown/execution-off labels;
real execution and account projections are not claimed tested by this UI.

2026-10-17 verification: full Python suite **386 passed, 1 skipped** (POSIX-only
flock), with the existing Starlette/httpx deprecation warning. New workbench
coverage accounts for 16 tests. `node --check web/workbench.js` and
`git diff --check` passed. Browser initial-page verification showed existing
SNDK history, execution off and inventory unverified. No live account request,
credential read or real order is part of this milestone's tests.
