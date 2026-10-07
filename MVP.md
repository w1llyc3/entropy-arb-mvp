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
  referral_mode: referred_t4   # referred_t4 | self_t3 | self_t4
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

A browser on this machine can start and stop record-only collection and run
the analyzer. The panel binds **127.0.0.1** only. It has no trading control,
no API-key form, and no `.env` editor. Start always launches:

```bash
python3 main.py --record-only --no-dashboard --symbol SNDK --hedge lighter
```

Symbol and hedge can be changed in the page. The default hedge is `lighter`.
The recorder's pid file is `.web/recorder.pid`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-web.txt
python3 -m web
```

Open <http://127.0.0.1:8765>. Optional: `python3 -m web --port 8765`.

The Analyze button runs exactly:

```bash
python3 tools/analyze.py --hours 24 --fees-bps 0.9 --min-samples 48
```

and shows the locked G1–G4 lines from that stdout (pass/fail on G1–G4).
SELL entropy net p50 and BUY entropy net p50 are contrast columns under
the gates, not Gate ids. Read the four numbers the same way as the table
in [Locked gates](#locked-gates).

Minute bars stay at `logs/minutes.csv` (one row per completed minute). The
status view reads that file for minutes collected, samples coverage, and the
latest top-of-book and fillable@$100 cells. It warns when the pid file's
process is gone or when recent minutes are thin or stale.
