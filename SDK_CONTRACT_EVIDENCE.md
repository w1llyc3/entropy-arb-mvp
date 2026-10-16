# Execution contract evidence — 2026-10-16

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
