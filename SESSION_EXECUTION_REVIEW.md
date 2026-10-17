# Session execution: pre-live code review candidate

Date: 2026-10-17. Real orders sent by this increment: **zero**.

This is a review checkpoint for inventory attribution rules, order reconciliation
and depth-based paper execution. It is **not approval for automatic live trading**
or evidence that a real Session has completed a round trip.

## What is implemented

- Frozen per-Session symbol, direction, mode, rule hash, fee/slippage assumptions,
  entry/exit thresholds, per-leg order/inventory limits, worst single-leg exposure
  limit, loss limit and duration. Default is OBSERVATION; PAPER is an explicit
  virtual-account selection. LIVE is rejected.
- Independent public depth for the single active paper Session. HL uses a
  one-second-minimum REST poll, validating returned coin and source timestamp.
  Lighter reuses the existing snapshot/diff/nonce-gap-aware websocket feed.
  Neither side writes collection CSVs or operates recorder controls.
- Freshness <=5 seconds, paired timestamp skew <=2 seconds, positive finite
  noncrossed depth. Stale/missing/invalid depth blocks new paper execution.
- Directional executable entry spread: `(short bid / long ask - 1) * 10000`.
  Subtract a conservative buffer of `2 * (entropy_fee + lighter_fee) +
  4 * slippage + funding_buffer` before comparing with the entry threshold.
  Exit compares `(short ask / long bid - 1) * 10000` with the residual threshold.
  These are paper model conventions, not a promise of real round-trip returns.
- IOC simulation walks available price levels inside the limit, models adverse
  slippage bounded by that limit, records cumulative fills and cancels the
  remainder. Paper base quantities use an explicit 0.000001 step; real venue lot,
  minimum notional, contract multiplier and margin constraints must be validated
  separately before live support, per asset.
- Intent/client IDs committed before the simulated send boundary. IDs are stable
  by Session/book sample/action/venue. Repeated ticks cannot resend the same plan.
  Simulated exchange truth and confirmed fill evidence are separate. An accepted
  order with lost acknowledgement remains UNKNOWN until queried; an absent result
  after a send boundary is never inferred failed and never blindly resent.
- Partial one-leg results trigger reductions of known paper inventory, not a
  compensating new hedge. Unknown outcomes block execution first. Pausing stops
  entries while exits/reductions continue. End or duration expiry enters DRAINING
  if virtual inventory/orders remain; final paper report requires flat inventory
  and reconciled terminal orders. A loss-limit breach pauses entries and reduces
  known virtual inventory on fresh depth.
- Restart interrupts active paper sessions, aborts only proven PREPARED/unsent
  intents and requires explicit query/resume. Scheduler/clock discontinuity
  invalidates books and requires review. Real Windows power events are untested.

## Inventory ownership and reconciliation

`session_reconciliation.py` matches durable claims to exchange evidence by venue,
account domain, symbol, stable client ID, side, quantity and reduce-only flag.
It deduplicates trades by venue/account/trade ID, rejects conflicting duplicates,
checks cumulative fills and terminal status, and compares each leg independently
against baseline plus owned fills. It does not net different contracts/accounts
or adopt foreign orders. Missing orders/history/positions remain incomplete.

The paper runtime uses this contract with its persistent fake exchange data.
Real account observations remain a distinct source. Session baselines are only
captured from both sides' fresh observations acquired after Session start. Later
position changes are UNATTRIBUTED_CHANGE, not owned inventory. Account-domain
changes invalidate comparisons rather than subtracting different accounts.

No real Session intents have been sent, so there are no real order/fill links to
claim. The current public Lighter position reader cannot enumerate and prove
complete active orders; the UI/report explicitly remain incomplete. The generic
matching contract is tested, but an authenticated real Session reconciler and
real execution ownership integration are **not connected to this workbench**.
The separate reviewed manual SNDK CLI remains the real contract-test candidate.

## Reports and UI

The virtual ledger shows each venue's paper inventory, pending/unknown orders,
holding time, turnover, fees, conservative marked signal and flat-only realized
PnL **before funding**. Configured funding buffer filters entries; it is not an
actual funding charge. Slippage/fees are assumptions, not observed fee tiers.

Simulated fills have separate entry/reduction edge histograms (5 bps bins) and
venue-specific price histograms (bin width = 0.1% of first confirmed fill price).
The end report retains rules/hash, lifecycle events, all virtual orders/fills,
reconciliation and distributions. Real inventory/PnL/fees/funding/settlement flags
remain unknown/unverified. A paper profit must never appear as real account PnL.

Workbench SQLite schema v2 is separate from execution ledger schema v3. A unique
SQLite backup (including WAL) precedes v1 migration. Existing rules, IDs and
immutable reports are retained. Do not restore a backup after new evidence has
been recorded merely to bypass a recovery or test budget.

## Collection continuity

Only workbench port 8770 is restarted to load this increment. Collector port 8766
and its process/CSV files are not restarted or modified. No new HL websocket is
opened: official documentation limits connections per IP, so this avoids
competing with the existing ten-asset collector. The extra REST request rate and
one Lighter market-data subscription are still shared infrastructure load.

Source: [Hyperliquid rate and connection limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits).

## Evidence classification

Final offline validation: **446 Python tests passed, 1 skipped** (POSIX-only
flock), with the existing Starlette/httpx deprecation warning. **5 Node UI
harness tests passed**. JS syntax and whitespace checks passed. The new increment
adds 34 Python cases beyond the previous 412-test milestone. No UI harness or
fixture order is a real order. These results are not browser visual acceptance.

**Confirmed by Code / offline tests:** stable intents, repeat-click/tick safety,
pause-before-send, partials, lost acknowledgements, crash after send boundary,
no resend, fees/cost filters, risk caps/loss stop, stale/skewed/invalid depth,
identity attribution, reconciliation gaps, drain/report lifecycle and v1 backup.

**Confirmed by Runtime Read-only Check:** bounded SNDK public feed check returned
fresh paired depth after the HL REST change, with accounts_loaded=false and
real_orders_sent=0. Local HTTP confirms collector PID 26504 continues RECORD_ONLY
and 8770 remains live_enabled=false / execution_connected=false.

**Requires Runtime Validation:** in-app browser rendering/interaction (CDP
connection timed out); actual Windows sleep/resume; long-running stability of
this new paper Session worker; authenticated real active/inactive/fill completeness;
actual order/partial/cancel/settlement semantics. Previous ce84cbe fake soak is
on another commit/module and cannot qualify this worker as a 24-hour pass.

## What review may approve

Review may approve continued paper use and, after remaining runtime checks, a
separate explicitly bounded **manual SNDK** real contract test through
`MANUAL_LIVE_TEST.md`. It must not turn this workbench's PAPER start into LIVE.
Exact reviewed commit, clean checkout, explicit opt-in/ack, real account ownership,
current fee checks and existing $25-per-leg manual test limits remain required.
No env live switches are changed by this increment, no main merge is performed,
and no existing real positions are automatically adopted or flattened.

Automatic real Sessions, other assets' execution adapters, passive quoting,
unattended cancellation/hedging policies and a final real settlement report
require subsequent implementation and review. These are not hidden behind a
disabled button or claimed complete by the paper test results.
