# entropy-arb-mvp

Measurement fork of [your-quantguy/entropy-arb](https://github.com/your-quantguy/entropy-arb)
(MIT, copyright 2026 yourQuantGuy — see [LICENSE](LICENSE)). This tree records
the SNDK Entropy vs Lighter books and scores a fillable-edge gate. It does
not add trading logic. Rebates are accrual-only and are never treated as
realized cash.

Record-only runs without API keys.

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter
python3 tools/analyze.py --hours 24 --fees-bps 0.9 --min-samples 48
```

`config.yaml` is the committed strategy (same values as `config.example.yaml`).
Credentials stay in `.env` and are required only for live orders.

## Locked config vs upstream

| key | this fork | upstream default |
|---|---|---|
| `entropy.taker_fee_bps` | **0.9** (must not be 0) | 0.0 |
| `hedge.taker_fee_bps` | 0.0 | 0.0 |
| `entropy` / `hedge` `max_position_usd` | 100 | 1000 |
| `sizing.take_fraction` | 0.2 | 0.5 |
| `sizing.max_order_notional_usd` | 100 | 500 |
| `sizing.min_order_notional_usd` | 10 | 10 |
| `execution.premium_persist_sec` | 3.0 | 0.3 |
| `execution.cooldown_sec` | 2.0 | 0.0 |
| `thresholds.midline_bps` | 0 (placeholder) | — |
| `thresholds.upper_bps` / `lower_bps` | 6.0 (placeholders; `analyze` replaces them) | — |

`fees_ledger` (unknown keys anywhere in the file are still startup errors):

```yaml
fees_ledger:
  referral_mode: referred_t4   # referred_t4 | self_t2 | self_t3 | self_t4
  rebate_accrual_only: true    # false is rejected
  growth_haircut: 0.90         # must be in [0.90, 1.0]
```

The loader also rejects `entropy.taker_fee_bps <= 0`. Omitting that key
defaults to 0.9 rather than 0.

## What the recorder adds

Sampling stays ~1/sec, one CSV row per minute. Existing top-of-book columns
are unchanged. New columns are appended. A CSV whose header does not match
is rotated to `logs/minutes.csv.old`.

On every fresh sample the recorder walks both books to $50 / $100 / $250
notional with `walk_depth` and `crossable_base` (matched base qty, both
legs' notionals ≥ target). The minute cell is the **mean** of samples that
could fill that size. Empty means the book was too thin for every sample
in the minute.

| column | meaning |
|---|---|
| `fill_{sell,buy}_edge_{50,100,250}_bps` | marginal executable edge, **pre-fee** |
| `slip_{sell,buy}_{50,100,250}_bps` | top-of-book edge minus average-fill edge |
| `depth_ok_frac` | fraction of samples where **both** directions fill ≥ $100 |
| `entropy_funding`, `hedge_funding` | last successful public REST poll, else blank |

`sell` is SELL entropy / BUY hedge. `buy` is BUY entropy / SELL hedge.
Same price ratios as the existing top-of-book edges, at the marginal price
after the walk.

### Funding

Upstream book feeds (`entropy_arb/feeds.py`) do not carry funding rates.
While the recorder is running, `FundingPoller` calls public REST every 60s
and, on success, stores the number on `OrderBook.funding`. The minute row
keeps the last sample. No rate is synthesized. A failed poll logs the
reason. The cell stays blank until the first success. A later failure keeps
the last real value and logs why the refresh failed.

- Entropy / Hyperliquid HIP-3: `POST https://api.hyperliquid.xyz/info` with
  `{"type":"metaAndAssetCtxs","dex":"<dex>"}` (Entropy dex is `io`). The
  matching asset context's `funding` field is stored unscaled.
- Lighter: `GET {profile}/api/v1/funding-rates` (mainnet host
  `https://mainnet.zklighter.elliot.ai`). The row with `exchange=lighter`
  and this market's `market_id` supplies `rate`, stored unscaled. Binance,
  Bybit, and Hyperliquid rows in that payload are ignored.

`tools/analyze.py` prints `entropy_funding - hedge_funding` per session
when both cells are present (`--by-session`). The two APIs are not
rescaled onto one funding period.

## Gate vs accrual (analyze)

`tools/analyze.py` defaults `--fees-bps` to **0.9** (Entropy 0.9 + Lighter 0
for SNDK). Rebate is forced to **0** on every gate. If the CSV has
`fill_*_edge_100_bps`, firing stats, gates, and the threshold suggestion use
those minute means. If the columns are absent it falls back to
`sell_edge_max_bps` / `buy_edge_max_bps` and warns.

For fillable@$100 it prints median (**p50**) in three columns. The middle
column is a **contrast**, not a Gate id:

1. **Pre-fee edge** — recorded marginal edge.
2. **Net-fee edge** = pre-fee − `--fees-bps`, **rebate forced to 0**.
   Printed again under the gates as **SELL entropy net p50** and **BUY
   entropy net p50**.
3. **Accrual-rebate edge (display only)** = net-fee edge + a haircut rebate.
   Not an input to thresholds and not cash.

### Locked gates

SELL and BUY net p50 are never G1 or G2. Each gate is one number.

| id | rule | number to read |
|---|---|---|
| **G1** | Conservative fillable@$100 net-fee median **> 0**. The value is the **worse** of SELL net p50 and BUY net p50 (fees = `--fees-bps`, rebate 0). A missing side fails. | signed bps. **PASS** iff `> 0`. Answers “is net edge positive?” |
| **G2** | Robustness. Upper/lower bases are the **p90** of fee-adjusted room (rebate 0), **before** the 1 bps floor on the pasted suggestion. Shift each base to **×0.5** and **×1.5**. A minute fires when its room is at least that hurdle; the firing’s net edge is the room. **PASS** iff every firing is **≥ 0**. | worst firing net edge, signed bps. **PASS** iff `≥ 0`. `n/a` and **PASS** when nothing fired. |
| **G3** | On each side, slip@$100 **p90 <** that side’s net-edge p50. **PASS** only when both sides pass (strict `<`). | worse slack `net p50 − slip p90`, signed bps. **PASS** iff slack `> 0`. The line is the comparison, not slip alone. |
| **G4** | Shallow book / sizing signal. `thin_frac` is the share of minutes with `depth_ok_frac` = 0 (too thin on every sample). **PASS** iff `thin_frac < 0.30`; otherwise **FAIL**, including a missing depth column. Mean `depth_ok_frac` (both directions fillable at ≥ $100) is still printed. The signal does not stop collection and does not change orders. | `thin_frac` in `[0, 1]`. **PASS** iff `< 0.30`. Mean `depth_ok_frac` sits beside it. |

Rebate math (display only), from
[Entropy referrals](https://docs.entropy.io/about-entropy/referrals):

- HIP-3 splits the fee evenly, so Entropy's share = 50% of `entropy.taker_fee_bps`.
- `growth_haircut` 0.90 keeps 10% of that share.
- `referred_t4` referred-user benefit = 100% of Entropy's share.
  `self_t3` = 160% and `self_t4` = 200% of the same share (early-bird table).
- Shipped config: `0.9 × 0.50 × (1 − 0.90) × 1.00 = 0.045` bps recognized.

`--midline auto` (the default) is the p50 of minute-close premium, rounded
to 0.1 bps. G1–G3 are then scored on edges relative to that center (sell
edge minus midline, buy edge plus midline). `--midline 0` is the historical
zero-center reading. At midline 0, G1–G4 match the table above. G4 does not
use the midline. Upper/lower suggestions
are the p90 of fee-adjusted room (rebate 0) on the fillable@$100 series
when that series exists, then floored at 1 bps for the pasted snippet.
G2 uses that same p90 **before** the floor.

## Localhost panel

A browser on this machine runs the SNDK · Entropy (dex=`io`) ↔ Lighter probe.
The panel binds **127.0.0.1** only. There is no host flag and no API-key
form. Credentials stay in the server `.env` and are never sent to the
browser. Opening the page does **not** arm live trading.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-web.txt
python3 -m web
```

Open <http://127.0.0.1:8765>. Optional: `python3 -m web --port 8765`.

The CLI live command is unchanged and is separate from the panel. It still
sends real orders once feeds are fresh:

```bash
python3 main.py --symbol SNDK --hedge lighter
```

### Click path (Dexter)

The Decision Card is **NO EDGE**. This probe is a user-chosen $10 minimum,
not a signal that the gate passed. The form does not switch the midline
when the session changes.

1. Fill `.env` on the server before any live click. Required names:
   `HL_PRIVATE_KEY`, `HL_ACCOUNT_ADDRESS`, `LIGHTER_ACCOUNT_INDEX`,
   `LIGHTER_API_KEY_INDEX`, `LIGHTER_API_PRIVATE_KEY`. The page never
   receives those values. Incomplete `.env` refuses both **启动** of
   探针实盘 and **确认**.
2. Open <http://127.0.0.1:8765>. **创建套利任务** (eyebrow NEW STRATEGY).
   The form opens on the Decision Card: pair SNDK, Entropy dex `io` ↔
   Lighter, midline **-1.7**, upper/lower **1.0 / 1.0**, order **$10**,
   position cap **$10** per side, fees **0.9 / 0** (read-only), referral
   **self_t2** accrual **≈ 0.54 bps** labeled **未到账**, manual confirm
   **on**, US RTH window **on** (recommended). Mode defaults to **只记录**.
   Creating the task does not arm live and does not send an order.
3. If midline is moved off -1.7, or manual confirm is turned off, the form
   shows **会偏离 Decision Card**. Confirm-off refuses to arm 探针实盘 and
   cannot silently auto-fire. Order and position inputs stay capped at $10.
4. Choose **探针实盘**, then **启动**. That is the explicit second action.
   It still launches only the recorder:

```bash
python3 main.py --record-only --no-dashboard --symbol SNDK --hedge lighter --config .web/probe.yaml
```

   With the RTH toggle on (the default), **启动** of 探针实盘 is refused
   outside US RTH (America/New_York, Monday–Friday 09:30 inclusive to
   16:00 exclusive). **只记录** does not need credentials.
5. Task-card statuses are **记录中 / 暖机 / LIVE / 暂停 / HALT / 已停止**.
   **暂停** freezes the recorder and refuses confirm. **停止** ends it.
   **对账** writes a local request and does not call a venue. **HALT** is
   red: a single-leg fill, or any net exposure that is not zero, blocks
   new opens.
6. When 探针实盘 is armed and the latest minute clears the band, a confirm
   card opens. It shows net edge **after 0.9 bps** (rebate not included)
   and, on a separate line, Tier2 Self accrual **≈ 0.54 bps** marked
   **未到账**. It also shows the task midline, the RTH flag, each leg's
   direction / notional / available / isolated, and a tail-vs-median
   warning when the live net is outside the recent central 80%.
7. **Inside RTH:** one click on **确认** admits a confirm id and calls
   `Engine.execute_confirmed` (the same `send_taker` path as
   `python3 main.py` without `--record-only`). Both legs are sized at
   most $10. The result is written to the status line and `.web/probe.log`.
8. **Outside RTH:** Confirm stays enabled. The card must show the current
   session and that session's measured midline
   (`us_regular` **-1.7**, `us_post_overnight` **+2.6**, `asia` **-0.4**),
   the task's active midline (still **-1.7** during the post session is
   **偏离约 4 bps**), and the line **带宽 1 bps，错中枢风险大于费率缺口**.
   The first click does not send. The button becomes **强制确认**. The
   second click sends, and the log contains `force_confirm_outside_rth=true`.
   A request that sets the force flag without that first click is refused.
9. After the first fill the panel shows actual Entropy fee versus the
   assumed **0.90 bps**. A mismatch stops further confirms.
10. **取消** skips the card and does not send. No order is placed without
    an admitted confirm id.

The recorder pid file is `.web/recorder.pid`. The task, the confirm queue,
the risk file, and `probe.log` live in `.web/` and are not committed.

The Analyze button runs exactly:

```bash
python3 tools/analyze.py --hours 24 --fees-bps 0.9 --min-samples 48
```

and shows the locked G1–G4 lines from that stdout (pass/fail on G1–G4).
SELL entropy net p50 and BUY entropy net p50 are contrast columns under
the gates, not Gate ids. Read the four numbers the same way as the table
in [Locked gates](#locked-gates).

Minute bars stay at `logs/minutes.csv` (one row per completed minute). The
status view reads that file for minutes collected, samples coverage, the
latest top-of-book and fillable@$100 cells, and deviation versus the task
midline. It warns when the pid file's process is gone or when recent minutes
are thin or stale.

### Probe limits

Confirm sends a real dual-leg order through `Engine.execute_confirmed`.
The process **启动** starts is still record-only, so the strategy loop
does not auto-fire.

- **Credentials.** Live arm and confirm both refuse when any of the five
  `.env` keys above is blank. Values are not returned to the browser.
- **$10 caps.** Per-order and per-side position are the Decision Card
  defaults and are rejected above $10.
- **Single-leg HALT.** If the two fills leave a net beyond the engine
  tolerance, status goes red **HALT** and later confirms are refused
  while that net is not zero. The probe does not send an unconfirmed hedge.
- **Fee check.** The first fill that reports an Entropy fee is shown
  against 0.9 bps. A mismatch stops further confirms.
- **Outside RTH.** Confirm is not hard-disabled. It takes the two-click
  **强制确认** path and logs `force_confirm_outside_rth=true`. The default
  is still “仅美国 RTH” for **启动**. The task midline changes only when
  the form is edited. NYSE holidays are not on the calendar.
- **对账** is a log line in `.web/reconcile.json`. It does not flatten
  on-chain positions.
- **self_t2 display accrual is 0.54 bps** (`0.9 × 0.50 × 1.20`), gross, not
  cash. `recognized_rebate_bps` still applies the 0.90 growth haircut
  (0.054 bps) inside the analyzer. Neither number enters G1–G4 or the
  net edge on the confirm card.
- **Balances** are a public read when `.env` has an account address or
  Lighter index. Private keys are not returned. Position / isolated stay
  empty when the payload does not name SNDK.
- **One task.** The form cannot target another symbol or venue. Unattended
  live (manual confirm off) is refused. Auto-confirm is a separate switch
  and **defaults off**.
- **Close-on-revert.** A symmetric book proposes a reduce-only close only
  after premium is inside the task band (edges included) for 3 seconds.
  The rule is not a midline touch. The card says 平仓. One-leg failure
  still HALTs.
- **Round-trip gate.** Auto open and auto close both require expected edge
  after fees to cover Entropy open+close (default **1.8 bps**, or twice a
  measured fee). Do not use the one-way 0.9. Outside RTH, auto-confirm
  does not click **强制确认**.
- **Margin sizing.** Optional. Free margin × leverage × safety (0.80),
  clipped by the $10 hard max. Missing margin or leverage refuses. Entropy
  available is spot USDC. 10× isolated: about $11 notional locks about
  $1.11 margin.
- **Windows recorder.** The panel spawns the recorder with
  `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | DETACHED_PROCESS` so
  starting it does not share uvicorn's console group.
