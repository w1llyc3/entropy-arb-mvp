# Manual SNDK live-contract test candidate

This branch is for **human review before any real test**. The web/strategy
service remains RECORD_ONLY. Nothing in this document authorizes Codex to load
your private keys or send orders. The operator supplies `.env.live` and runs
commands only after reviewing the exact commit. This is not an automatic
arbitrage release or a claim that account-level settlement was verified.

## Scope and gates

- Separate persistent ledger: `.web/manual-live-test/ledger.sqlite`. Never
  copy/delete/reset it after a request may have been sent. Root and account
  OS locks prevent a second executor using the same account pair.
- One paired entry plus at most four explicitly prepared reduce-only exits for the
  lifetime of this ledger, each leg at most **$25**. The number is a hard upper
  bound, not a recommended test size. Use the smallest legal shared size.
- SNDK only, Entropy `io` and Lighter mainnet only. IOC **limit** orders, exact
  decimal grids and stable IDs; no market-average-price substitution.
- Both SNDK accounts must initially be flat with no outstanding/pending SNDK
  orders. Do not use another bot or trade these accounts during the test.
- At send time, fetch new account/book observations, verify the original
  position baseline, current precision/minimum, depth within the approved
  limit, collateral and a maximum 5bp limit-to-touch deviation. Price/quantity
  never change automatically. Old plans expire after ten minutes or code change.
- Live gates: clean checkout, reviewed commit equals HEAD, `ENTROPY_LIVE_TEST=YES`,
  and the explicit CLI acknowledgement. Only `send` and `cancel` can transmit.
- No automatic hedge/flatten. Unexpected positions, unknown orders, incomplete
  fills or history close the controlled path. Query original IDs. Never delete
  the ledger or retry with a different ID to get around a closed gate.

## 1. Install and review

Copy `.env.live.example` to `.env.live` locally. Both private file patterns are
gitignored. Use a dedicated account/subaccount and a scoped Lighter API key.
The Entropy public account is the master/subaccount address, not the agent
wallet address. No vault-signing override is implemented in this candidate;
do not use a vault that requires `vaultAddress`.

Use the isolated environment (PowerShell):

```powershell
.\.venv-audit\Scripts\python.exe -m venv .venv-live
.\.venv-live\Scripts\python.exe -m pip install -r requirements-live.txt
.\.venv-live\Scripts\python.exe tools/check_live_sdk.py
```

The SDK checker does not read env files or create account clients. It checks
exact Git install revisions, callable signatures and the Windows native DLL.
Keep `ENTROPY_LIVE_TEST=NO` during configuration and read checks. Set
`ENTROPY_REVIEWED_COMMIT` to the full commit you actually reviewed. Do not
automatically reset it to HEAD after changes.

## 2. Authenticated read checks, with no real orders

```powershell
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check --authenticated-read preflight
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check --authenticated-read query
```

`--authenticated-read` explicitly loads only Lighter API auth credentials,
creates a short-lived token in memory and sends read requests. It does not
load the Hyperliquid private key or provide a send wrapper. Without this flag,
reads use an optional `LIGHTER_READ_TOKEN` and never load signing credentials.
No token/key is printed or persisted. SDK logging is disabled in this CLI.

Verify account identity, flat positions, active/inactive history pagination,
and the `trades` response against UI/explorer. `LIGHTER_TRADE_FEE_SCALE` must
remain blank until the integer fee field's unit is independently verified
against a known trade. Do not assume `1000000` merely because USDC has six
decimals: the API schema does not describe fee units. Preparing a test is
blocked while the field is blank. Synthetic test fixtures are not this proof.

`query` records order/fill evidence. `positions_observed_match=true` means the
fresh observed quantities agree with accumulated fills. It is **not** a
settlement watermark: the report always states `settlement_verified=false`
and the automatic strategy remains disabled. Keep sanitized read evidence
for review; never share `.env.live`, auth headers or signed payloads.

## 3. Prepare before sending

Use current prices from each venue, a shared legal quantity, and a unique
command key. The following placeholders deliberately cannot be sent as-is:

```powershell
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check --authenticated-read prepare --key "test-entry-001" --buy entropy --qty "<shared quantity>" --price-entropy "<buy limit>" --price-lighter "<sell limit>"
```

This writes an intent, baselines and stable client IDs. It sends no orders.
Inspect the returned operation and both legs. Repeat inspection with `query`.
Do not prepare another entry if an earlier request exists; inspect the existing
operation. Existing IDs survive process restarts and SDK exceptions.

Only after your review and the preceding account checks, set the live env
switch to YES. Sending requires the operation ID returned above:

```powershell
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check send --operation "<operation ID>" --ack "I_REVIEWED_THIS_COMMIT_AND_ACCEPT_REAL_ORDERS"
```

A timeout, HTTP error, SDK error or acceptance acknowledgement remains UNKNOWN.
Run authenticated `query`; repeating `send` on a claimed operation cannot send
again. Do not interpret a printed response as a fill or assume zero quantity
after timeout. No blanket retry exists.

## 4. Cancellation and explicit exit

For a genuinely unresolved/open **managed** leg, cancellation has a separate
durable key. An IOC remainder normally has already canceled; terminal orders
need reconciliation rather than an unnecessary cancel:

```powershell
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check cancel --leg "<managed leg ID>" --key "test-cancel-001" --ack "I_REVIEWED_THIS_COMMIT_AND_ACCEPT_REAL_ORDERS"
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check --authenticated-read query
```

Lighter cancellation first resolves and validates the exchange `order_index`.
A cancel acknowledgement never implies zero fills. Recovery queries the
original IDs; a cancel racing a fill can legitimately finish FILLED.

Once exact terminal order/fill evidence and current positions agree, explicitly
prepare an exit. Its quantities come only from this test's observed, owned
positions; it cannot adopt an unexplained external position:

```powershell
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check --authenticated-read prepare-close --key "test-exit-001" --price-entropy "<exit limit>" --price-lighter "<exit limit>"
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check send --operation "<exit operation ID>" --ack "I_REVIEWED_THIS_COMMIT_AND_ACCEPT_REAL_ORDERS"
.\.venv-live\Scripts\python.exe -m entropy_arb.live_check --authenticated-read query
```

Verify both exchange UIs show the intended residual quantities, all order IDs
are terminal and no orders remain. The prepared exit is IOC and may partially
fill too. After fresh terminal-order/fill/position agreement, another explicit
`prepare-close` with a new key can reduce only the confirmed remainder (up to
four exit attempts in total). There is no automatic retry. Unknown outcomes,
unexplained positions, below-grid dust or an exhausted test budget require
operator resolution using confirmed exchange positions and reduce-only
controls. Do not assume the tool automatically unwinds a test.

Set `ENTROPY_LIVE_TEST=NO` afterwards. Preserve the ledger and evidence. Account
level validation is complete only after you review these results, not because
an offline suite passed.

## 5. Windows and long-duration evidence

Run these with the secret-free test environment, independent of real accounts:

```powershell
.\.venv-audit\Scripts\python.exe tools/windows_resume_probe.py --seconds 300 --output .web/windows-resume-result.json
.\.venv-audit\Scripts\python.exe tools/soak_execution.py --seconds 86400 --cycle-sec 30 --output .web/execution-soak-24h.json
```

For the resume probe, manually sleep the Windows device for >20 seconds while
the fake service is armed, then resume. It never submits, even fake orders.
No observed gap returns INCONCLUSIVE, not PASS. Confirm yourself that the gap
corresponded to actual sleep, rather than a scheduler stall/clock change.

The soak creates only fake accounts and a disposable DB. It exercises duplicate
confirmations, paired entry/exit and service restarts. Keep the output, including
any failure; shorter smoke runs do not qualify as 24h evidence. It does not
measure real exchange latency, permissions, fills or settlement finality.

## Remaining release boundary

Automatic live arbitrage is still blocked pending authenticated contract
fixtures, verified fee units/finality, real sleep results, 24h execution soak,
backup/migration rehearsal and PR review. This candidate supplies an explicitly
bounded manual test path for the operator to gather that evidence; it does not
weaken the default service's release gate or rely on local JSON as trade proof.
