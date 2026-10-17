# Execution contract evidence — 2026-10-16

## Follow-up, 2026-10-17 (supersedes earlier installation limitation)

- Both pinned SDKs installed in `.venv-live` after the installer temporarily
  used the existing Windows proxy. No system proxy setting changed. Native
  Windows AMD64 loading and SDK callable-signature checks passed.
- Existing parent-project env is explicitly supported with `--env-file ../.env`,
  including legacy HL names. Diagnostics disclose no values/fingerprints, and
  the user's env was not modified.
- Real read-only preflight observed flat SNDK positions/no outstanding orders.
  HL approved-agent identity and Lighter read auth passed. Local credentials
  were used only for identity/query authentication, not order signing/sending.
- Lighter inactive-order and trade endpoints returned real samples. HL fill
  history endpoint worked but had no SNDK fills in the 24h sample. History and
  settlement completeness are not inferred from endpoint/sample availability.
- Real querying exposed bool URL-encoding failure in `trades`; GET booleans now
  use lowercase strings. Errors expose endpoint path/status/category only,
  never auth headers, query values or response bodies. Auth tokens explicitly
  use the configured API-key index. HL authorization is rechecked before send.
- Current account fee schedule supports ZERO_ONLY. A missing conversion scale
  permits only exact-zero fees with a send-time schedule recheck, not guessed
  nonzero conversion. Missing/nonzero integrator fees block reconciliation.
- Unsent plans may be atomically abandoned without deleting ledger evidence;
  this cannot release a sent/unknown order. Soak output now retains its database,
  publishes running progress and hashes final DB evidence.

No real orders have been sent. Real Windows sleep and 24h execution soak results
are still pending; the normal strategy service remains RECORD_ONLY.

Follow-up regression: **370 Python tests passed, 1 skipped**, one existing
Starlette/httpx warning (28.77 seconds). Real identity/auth/history checks
were read-only; no order intents were submitted or transaction signatures
created. The zero-only account policy is not a claim about nonzero fee units.

## Confirmed by Code / public source inspection

- Official Hyperliquid SDK revision:
  `2fdb18f9517675ea03695a0962bd19eece9c83f0`.
  [Exchange source](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/2fdb18f9517675ea03695a0962bd19eece9c83f0/hyperliquid/exchange.py)
  and [info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint).
  The adapter signs an explicit IOC-limit action with a stable 128-bit cloid,
  persists nonce by signer address and has a five-second exchange expiry.
  It derives cumulative fills from exact order-linked trade records rather
  than treating canceled remaining size as proof of original fills.
- Official Lighter SDK revision:
  `5825eb19867a4dbdd47c02642dc1eec30c30ca73`.
  [Signer source](https://github.com/elliottech/lighter-python/blob/5825eb19867a4dbdd47c02642dc1eec30c30ca73/lighter/signer_client.py),
  [order schema](https://github.com/elliottech/lighter-python/blob/5825eb19867a4dbdd47c02642dc1eec30c30ca73/lighter/models/order.py),
  [trade schema](https://github.com/elliottech/lighter-python/blob/5825eb19867a4dbdd47c02642dc1eec30c30ca73/lighter/models/trade.py).
  The adapter uses LIMIT/IOC, integer wire prices/quantities, a collision-checked
  persistent client-index mapping, complete bounded cursor traversal and exact
  account/market/order/client/fill identities. Cancellation resolves the exchange
  order index first. A duplicate historical client index blocks sending before
  either leg crosses its send boundary. The application bounds client indices
  to 48 bits; accepted account-specific behavior still needs runtime validation.
- Only reviewed pins pass `verify_sdk_pins`; a different VCS revision or an
  unpinned wheel is rejected by the real signer factory.
- Additional safety finding fixed: locks now cover each venue/account as well
  as the original account pair. Sharing just one account cannot bypass ownership.
  The manual sender also locks the public HL signer address.

## Public endpoint runtime observations (no accounts / no signatures)

On 2026-10-16, `/info` `meta` for `io:SNDK` returned `szDecimals=4`,
`maxLeverage=15`; `perpDexs` resolved io at index 10. Lighter `orderBooks`
resolved SNDK market 139, size decimals 4, price decimals 2, minimum base
0.0035 and minimum quote 10. Its public `orderBookOrders` contained price and
remaining-base fields used by the test preflight. These values are observations,
not configuration constants: the runtime resolves catalogs again before tests.
No leverage change is made automatically.

## Not verified / release limitations

- Authenticated fixtures in `tests/test_live_*` are synthetic schema-based
  models, not recorded real account responses. The user will populate env and
  run the documented manual read checks after review.
- Lighter integer trade-fee units remain unverified. The caller must establish
  and configure the scale before preparing an order; unknown units are not zero.
- Neither local timestamps nor matching position observations are presented as
  a settlement watermark. Automatic live readiness remains false.
- Official SDK installation was attempted in an isolated `.venv-live`.
  GitHub SDK clone, archive and native-DLL transfers failed/reset/timed out.
  **Full installed-SDK and Windows native-load checks have not passed.**
  `tools/check_live_sdk.py` is supplied for the operator; source inspection is
  not a substitute for that check. Partial downloads are ignored runtime files.
- Real Windows sleep/resume and 24-hour execution-service soak remain pending.
  A 35-second simulated smoke run completed seven entry/exit cycles, one restart,
  14 operations, 28 fills and zero unknown/residual positions. It explicitly
  reports `qualifies_as_24h_soak=false`.

## User review boundary

Final code regression: **356 Python tests passed, 1 POSIX-only test skipped**,
one existing Starlette/httpx deprecation warning (23.57 seconds); chart JavaScript
**7 tests passed**. CLI end-to-end tests inject synthetic transports/signers and
cover initial plans, duplicate sends, exact identity conflicts, owned partial
exits and overlapping-account locks. They do not load real credentials.

The default UI/CLI collector never constructs these signers. The separate
manual test tool requires a clean reviewed commit and explicit send opt-in.
It does not authorize automated trading or declare the account contracts
validated. Preserve all result files and the test ledger for the user's
post-test review. See `MANUAL_LIVE_TEST.md` for commands and limits.
