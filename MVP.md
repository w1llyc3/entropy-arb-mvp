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
| `entropy_funding`, `hedge_funding` | blank unless a book already exposes numeric `funding` |

`sell` is SELL entropy / BUY hedge. `buy` is BUY entropy / SELL hedge.
Same price ratios as the existing top-of-book edges, at the marginal price
after the walk.

### Funding

Upstream book feeds (`entropy_arb/feeds.py`) do not carry funding rates.
The columns are written and left blank. No rate is synthesized. If a feed
later sets `OrderBook.funding` to a number (or a zero-arg callable that
returns one), the recorder stores the last sample in the minute.

## Gate vs accrual (analyze)

`tools/analyze.py` defaults `--fees-bps` to **0.9** (Entropy 0.9 + Lighter 0
for SNDK). If the CSV has `fill_*_edge_100_bps`, firing stats and the
threshold suggestion use those minute means. If the columns are absent it
falls back to `sell_edge_max_bps` / `buy_edge_max_bps` and warns.

For fillable@$100 it prints median (**p50**) in three columns:

1. **Pre-fee edge** — recorded marginal edge.
2. **GATE net-fee edge** = pre-fee − `--fees-bps`, **rebate forced to 0**.
   This is the only gate metric.
3. **Accrual-rebate edge (display only)** = gate + a haircut rebate.
   Not an input to thresholds and not cash.

Rebate math (display only), from
[Entropy referrals](https://docs.entropy.io/about-entropy/referrals):

- HIP-3 splits the fee evenly, so Entropy's share = 50% of `entropy.taker_fee_bps`.
- `growth_haircut` 0.90 keeps 10% of that share.
- `referred_t4` referred-user benefit = 100% of Entropy's share.
  `self_t3` = 160% and `self_t4` = 200% of the same share (early-bird table).
- Shipped config: `0.9 × 0.50 × (1 − 0.90) × 1.00 = 0.045` bps recognized.

G3 is slip@$100 **p90**. G4 is the mean of `depth_ok_frac`.

Midline is still the p50 of minute-close premium. Upper/lower suggestions
are the p90 of fee-adjusted room (rebate 0) on the fillable@$100 series
when that series exists.

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

and labels the result:

| id | number |
|---|---|
| G1 | SELL entropy GATE net p50 (pre-fee median − `--fees-bps`, rebate 0) |
| G2 | BUY entropy GATE net p50 (same rule) |
| G3 | slip@$100 p90 |
| G4 | mean `depth_ok_frac` |

Minute bars stay at `logs/minutes.csv` (one row per completed minute). The
status view reads that file for minutes collected, samples coverage, and the
latest top-of-book and fillable@$100 cells. It warns when the pid file's
process is gone or when recent minutes are thin or stale.
