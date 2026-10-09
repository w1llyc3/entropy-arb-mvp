"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import (  # noqa: E402
    ConfigError, display_accrual_bps, load_config, recognized_rebate_bps)

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.dashboard and cfg.log_file
    assert cfg.entropy.fee_bps == 0.9
    assert cfg.hedge.fee_bps == 0.0
    assert cfg.entropy.cap_usd == 100 and cfg.hedge.cap_usd == 100
    assert cfg.take_fraction == 0.2
    assert cfg.max_order_notional == 100 and cfg.min_order_notional == 10
    assert cfg.premium_persist_sec == 3.0 and cfg.cooldown_sec == 2.0
    assert cfg.midline_bps == 0.0 and cfg.upper_bps == 6.0 and cfg.lower_bps == 6.0
    assert cfg.referral_mode == "referred_t4"
    assert cfg.rebate_accrual_only is True
    assert cfg.growth_haircut == 0.90
    shipped = load_config(os.path.join(ROOT, "config.yaml"), NO_ENV,
                          symbol="SNDK", hedge_venue="lighter")
    assert shipped.entropy.fee_bps == 0.9
    assert shipped.referral_mode == "referred_t4"
    assert abs(recognized_rebate_bps(0.9, 0.90, "referred_t4") - 0.045) < 1e-12


def test_utf8_nonascii_comment_loads():
    import builtins
    text = MINIMAL + "# 中文注释：中枢占位\n"
    f = tempfile.NamedTemporaryFile("wb", suffix=".yaml", delete=False)
    f.write(text.encode("utf-8"))
    f.close()
    seen = []
    real_open = builtins.open

    def tracking_open(file, *args, **kwargs):
        if os.path.abspath(str(file)) == os.path.abspath(f.name):
            seen.append(kwargs.get("encoding"))
        return real_open(file, *args, **kwargs)

    builtins.open = tracking_open
    try:
        cfg = load_config(f.name, NO_ENV, symbol="SNDK", hedge_venue="lighter")
    finally:
        builtins.open = real_open
        os.unlink(f.name)
    assert seen == ["utf-8"]
    assert cfg.symbol == "SNDK"
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True
    # omitted entropy fee must not silently become 0
    assert cfg.entropy.fee_bps == 0.9
    assert cfg.referral_mode == "referred_t4"
    assert cfg.rebate_accrual_only is True
    assert cfg.growth_haircut == 0.90


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


def test_zero_entropy_fee_rejected():
    expect_error(MINIMAL + "\nentropy:\n  taker_fee_bps: 0\n",
                 "taker_fee_bps must be > 0")


def test_fees_ledger_validation():
    expect_error(MINIMAL + "\nfees_ledger:\n  referral_mode: referred_t4\n"
                 "  rebate_accrual_only: true\n  growth_haircut: 0.90\n"
                 "  cash_now: true\n",
                 "fees_ledger.cash_now")
    expect_error(MINIMAL + "\nfees_ledger:\n  referral_mode: vip\n"
                 "  rebate_accrual_only: true\n  growth_haircut: 0.90\n",
                 "referral_mode")
    expect_error(MINIMAL + "\nfees_ledger:\n  referral_mode: referred_t4\n"
                 "  rebate_accrual_only: false\n  growth_haircut: 0.90\n",
                 "rebate_accrual_only")
    expect_error(MINIMAL + "\nfees_ledger:\n  referral_mode: referred_t4\n"
                 "  rebate_accrual_only: true\n  growth_haircut: 0.5\n",
                 "growth_haircut")
    cfg = load(MINIMAL + "\nfees_ledger:\n  referral_mode: self_t4\n"
               "  rebate_accrual_only: true\n  growth_haircut: 1.0\n")
    assert cfg.referral_mode == "self_t4" and cfg.growth_haircut == 1.0
    assert recognized_rebate_bps(0.9, 1.0, "self_t4") == 0.0
    cfg = load(MINIMAL + "\nfees_ledger:\n  referral_mode: self_t2\n"
               "  rebate_accrual_only: true\n  growth_haircut: 0.90\n")
    assert cfg.referral_mode == "self_t2"
    assert abs(display_accrual_bps(0.9, "self_t2") - 0.54) < 1e-12
    assert abs(recognized_rebate_bps(0.9, 0.90, "self_t2") - 0.054) < 1e-12


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
