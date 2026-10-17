# Pre-live validation in progress — 2026-10-17

**Not released. No real orders sent. Collector must continue running.**

## Completed in this increment

- Authenticated, bounded SNDK inactive-order pagination twice, with a stable
  history fingerprint. Three sampled filled orders matched unique trade IDs,
  side/account/market/client IDs and cumulative quantities. API exposed four
  inactive orders and zero current SNDK active orders during observation.
- Real responses omitted trade fee and integrator-fee fields. They remain unknown,
  never zero-filled. Cursor exhaustion covers the API-exposed rows, not an
  unqualified claim about complete account history or settlement finality.
- Separate execution-quantity evidence from financial fills. Unknown fees still
  block financial completion. Explicit manual reduce-only exits can use fresh,
  exact managed-order/fill quantities plus matching exchange positions, without
  fabricating fee rows. New entries/settlement/reservation release are not granted
  by that exception. Canceled/partial outcomes still require exact evidence;
  missing or conflicting identities cannot use an older cached quantity proof.
- First manual real entry now checks retained runtime evidence **before loading
  real transaction signers**. Missing, running, short, unrelated-source, wrong
  account or stale evidence blocks it. Replaying an already-claimed operation
  returns the existing operation without constructing signers or resending.
- Existing exact-reviewed-commit/clean-checkout/opt-in/ack checks, one-entry
  lifetime budget and $25 per-leg cap remain. Reductions and cancels do not get
  trapped behind a 24-hour evidence freshness gate; their independent account,
  intent, quantity, reduce-only, depth and operator acknowledgement checks remain.

## Evidence pipeline

1. `tools/soak_session.py` exercises the actual workbench lifespan/API with
   virtual depth, durable orders and its own SQLite database. Every cycle creates
   a paper Session, replays duplicate commands, opens/exits, verifies flatness
   and checks its report. Restarts every five cycles and lost acknowledgements
   every seven cycles exercise recovery without replaying sends. Default 24h.
   Retain the database, source fingerprint, commit, PID and atomic progress file.
   A short passing smoke test does not qualify as a 24-hour pass.
2. `tools/observe_session_resume.py` runs a separate virtual Session and waits
   for **natural** Windows sleep/resume. It never invokes sleep, changes power
   settings or touches collector controls. A clock/scheduler gap alone cannot
   pass: require recovery/invalid books/no virtual orders and matching Windows
   Kernel-Power sleep/wake events with at least 15 seconds between them.
   No sleep, unavailable event logs or unsupported event evidence is INCONCLUSIVE.
3. `tools/prelive_readonly.py --env-file ../.env` runs under `.venv-live`,
   collecting pinned SDK/native validation, fresh account/fee preflight and
   history audit. Only Lighter read authentication is loaded; no HL transaction
   key or order submission is used. Results contain summaries, not account rows,
   tokens or key material. Account domain is bound by a public-identifier digest.
4. `tools/build_prelive_report.py --soak <path> --resume <path>` re-evaluates
   the artifacts and writes `.web/prelive-validation/release.json`. The CLI
   re-evaluates the underlying artifacts again on first entry, rather than
   trusting a ready flag. Source fingerprints must still match; the retained
   ledger hash must agree; real resume proof is required; read-only checks must
   be at most 60 seconds old and refer to the current account domain.

These checks qualify only a separately reviewed, bounded **manual SNDK first
entry experiment**, never automatic Sessions or other assets' live execution.
Unknown historical fees remain unknown and the live account's current fee policy
is rechecked independently. Real submission/cancel/settlement behavior is what
the later human-approved small contract test will measure, not something this
no-order validation can claim to have exercised.

## Current validation evidence

- Full offline suite: **470 passed, 1 skipped** (POSIX flock), existing
  Starlette/httpx warning. Frontend script harness: **5 passed**.
- New workbench smoke: 15 cycles, 3 restarts, 2 lost-ack recoveries, no failures;
  explicitly NOT a 24-hour pass. Failed initial harness attempts are retained
  as failed evidence; their command-key/market-fixture issues were corrected.
- Real SDK/native Windows checks passed. At observation, SNDK was flat with no
  active orders and the current ZERO_ONLY account fee policy was allowed.
- Historical sample execution identities/quantities matched; fee fields and
  real settlement finality remain unverified.
- Windows recent-event read found no usable sleep/resume evidence. No sleep
  was initiated. Do not infer a resume pass merely because Windows is running.

## Constraint that cannot be bypassed

An intentional physical sleep of this machine interrupts network data collection.
The instruction to keep collection running therefore excludes initiating it.
The observer can capture a naturally occurring event, but if none occurs, the
real-sleep gate remains unverified. A maintenance window explicitly authorized
later would be needed to force that test. No waiver is inferred from elapsed
time, simulated clock tests, successful SDK loading or an ordinary process restart.

The new 24-hour workbench soak is also an elapsed-time gate. The older ce84cbe
TradingService soak cannot substitute for the new Session worker or for changed
execution-adapter code. Do not request final live release review while these
required runtime gates are still unqualified.
