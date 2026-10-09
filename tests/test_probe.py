"""RTH gate, confirm payload, and the SNDK probe session.

Run:  python3 -m pytest tests/test_probe.py
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import load_config  # noqa: E402
from web.accounts import read_accounts, scrub  # noqa: E402
from web.live_exec import execute_admitted  # noqa: E402
from web.probe import (  # noqa: E402
    ACCRUAL_BPS, ACCRUAL_LABEL, CONFIRM_FIELDS, DECISION_WARNING,
    FORCE_RISK_LINE, build_confirm_payload, confirm_field_errors,
    decision_warnings, in_us_rth, leg_plan, normalize_task,
    session_snapshot,
)
from web.recorder_ctl import RecorderControl  # noqa: E402
from web.session import ProbeError, ProbeSession  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _ts(moment: datetime) -> float:
    return moment.timestamp()


INSIDE = _ts(datetime(2026, 1, 7, 10, 0, tzinfo=NY))   # Wednesday 10:00 EST
OUTSIDE = _ts(datetime(2026, 1, 7, 16, 0, tzinfo=NY))  # Wednesday 16:00 EST
NO_ENV = os.path.join(os.path.dirname(__file__), "no-such.env")


def test_rth_window_bounds_and_weekend():
    assert in_us_rth(datetime(2026, 1, 7, 9, 29, tzinfo=NY)) is False
    assert in_us_rth(datetime(2026, 1, 7, 9, 30, tzinfo=NY)) is True
    assert in_us_rth(datetime(2026, 1, 7, 15, 59, tzinfo=NY)) is True
    assert in_us_rth(datetime(2026, 1, 7, 16, 0, tzinfo=NY)) is False
    assert in_us_rth(datetime(2026, 1, 10, 12, 0, tzinfo=NY)) is False  # Saturday
    # 13:30 UTC is 09:30 EDT on this Wednesday. 13:29 is still before the open.
    assert in_us_rth(datetime(2026, 7, 8, 13, 29, tzinfo=UTC)) is False
    assert in_us_rth(datetime(2026, 7, 8, 13, 30, tzinfo=UTC)) is True
    assert in_us_rth(datetime(2026, 7, 8, 19, 59, tzinfo=UTC)) is True
    assert in_us_rth(datetime(2026, 7, 8, 20, 0, tzinfo=UTC)) is False


def test_missing_new_york_zone_names_tzdata(monkeypatch):
    import web.probe as probe
    monkeypatch.setattr(probe, "_NY", None)

    def missing(key):
        raise ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(probe, "ZoneInfo", missing)
    with pytest.raises(ZoneInfoNotFoundError, match="pip install tzdata"):
        in_us_rth(datetime(2026, 1, 7, 15, 0, tzinfo=UTC))


def test_decision_card_defaults_and_warning():
    spec = normalize_task({})
    assert spec["symbol"] == "SNDK"
    assert spec["entropy_dex"] == "io"
    assert spec["hedge"] == "lighter"
    assert spec["midline_bps"] == -1.7
    assert spec["upper_bps"] == 1.0 and spec["lower_bps"] == 1.0
    assert spec["order_notional_usd"] == 10
    assert spec["max_position_usd"] == 10
    assert spec["entropy_fee_bps"] == 0.9
    assert spec["lighter_fee_bps"] == 0.0
    assert spec["referral_mode"] == "self_t2"
    assert spec["manual_confirm"] is True
    assert spec["rth_only"] is True
    assert abs(ACCRUAL_BPS - 0.54) < 1e-9
    assert decision_warnings(spec) == []
    shifted = normalize_task({"midline_bps": 0})
    assert decision_warnings(shifted) == [DECISION_WARNING]
    quiet = normalize_task({"manual_confirm": False})
    assert decision_warnings(quiet) == [DECISION_WARNING]
    with pytest.raises(ValueError):
        normalize_task({"symbol": "BTC"})
    with pytest.raises(ValueError):
        normalize_task({"hedge": "lighter-rh"})
    with pytest.raises(ValueError, match="cap is \\$10"):
        normalize_task({"order_notional_usd": 11})
    with pytest.raises(ValueError, match="cap is \\$10"):
        normalize_task({"max_position_usd": 25})


def _write_env(root, *, complete=True):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if complete:
        text = (
            "HL_PRIVATE_KEY=test-key\n"
            "HL_ACCOUNT_ADDRESS=0xabc\n"
            "LIGHTER_ACCOUNT_INDEX=1\n"
            "LIGHTER_API_KEY_INDEX=2\n"
            "LIGHTER_API_PRIVATE_KEY=test-lighter\n"
        )
    else:
        text = "HL_PRIVATE_KEY=test-key\n"
    (root / ".env").write_text(text, encoding="utf-8")


def _flat_result(req, *, fee=0.9, buy=0.01, sell=0.01):
    assert req.get("confirm_id")
    net = buy - sell
    return {
        "ok": abs(net) < 1e-9 and abs(fee - 0.9) <= 0.05,
        "routed": True,
        "sent": True,
        "halted": abs(net) > 1e-9,
        "halt_reason": "single-leg fill" if abs(net) > 1e-9 else None,
        "confirm_id": req["confirm_id"],
        "buy_fill": buy,
        "sell_fill": sell,
        "net_base": net,
        "entropy_fee_bps": fee,
        "status": "filled/filled" if sell else "filled/canceled",
        "error": None,
    }


def _session(tmp_path, now, *, env=True, executor=None, calls=None):
    if env:
        _write_env(tmp_path)
    box = calls if calls is not None else []

    def _exec(req):
        box.append(req)
        return _flat_result(req)

    return ProbeSession(tmp_path, RecorderControl(tmp_path), now=lambda: now,
                        account_reader=lambda: {
                            "creds": {"entropy": False, "lighter": False},
                            "entropy": {"equity": None, "available": 25.0,
                                        "position": 0.0, "isolated": True},
                            "lighter": {"equity": None, "available": 40.0,
                                        "position": 0.0, "isolated": False},
                            "note": None,
                        },
                        executor=executor or _exec)


def _arm(sess, *, rth_only=True, manual_confirm=True, midline=-1.7):
    sess.save_task({
        "mode": "live",
        "manual_confirm": manual_confirm,
        "rth_only": rth_only,
        "midline_bps": midline,
        "upper_bps": 1.0,
        "lower_bps": 1.0,
        "order_notional_usd": 10,
        "max_position_usd": 10,
    })
    spec = sess.task()
    spec["live_armed"] = True
    sess._write_json(sess.task_path, spec)
    proposal = {
        "proposal_id": "probe-1",
        "direction": "sell_entropy",
        "pre_fee_edge_bps": 2.5,
        "net_edge_bps": 1.6,
        "legs": leg_plan("sell_entropy", 10, sess._accounts()),
        "tail_vs_median": "尾部相对中位数：当前净边际 +1.60 bps，中位数 +0.10 bps",
    }
    sess._save_queue({"pending": proposal, "history": []})
    return proposal


def test_confirm_payload_fields_and_separate_accrual():
    spec = normalize_task({"mode": "live"})
    proposal = {
        "proposal_id": "probe-1",
        "direction": "sell_entropy",
        "pre_fee_edge_bps": 2.5,
        "net_edge_bps": 1.6,
        "legs": leg_plan("sell_entropy", 10, {
            "entropy": {"available": 12.5, "isolated": True},
            "lighter": {"available": 9.0, "isolated": False},
        }),
        "tail_vs_median": "尾部相对中位数：当前净边际 +1.60 bps，中位数 +0.10 bps",
    }
    payload = build_confirm_payload(spec, proposal, rth=True,
                                    tail=proposal["tail_vs_median"])
    for key in CONFIRM_FIELDS:
        assert key in payload
    assert payload["fee_bps"] == 0.9
    assert payload["net_edge_bps"] == 1.6
    assert abs(payload["net_edge_bps"] - (2.5 - 0.9)) < 1e-9
    assert payload["accrual_bps"] == pytest.approx(0.54)
    assert payload["accrual_label"] == "未到账"
    assert payload["accrual_label"] == ACCRUAL_LABEL
    assert payload["referral_mode"] == "self_t2"
    assert payload["midline_bps"] == -1.7
    assert payload["rth"] is True
    assert payload["confirm"] is True
    assert payload["tail_vs_median"].startswith("尾部相对中位数")
    venues = [leg["venue"] for leg in payload["legs"]]
    assert venues == ["Entropy", "Lighter"]
    for leg in payload["legs"]:
        for field in ("venue", "direction", "notional_usd", "available", "isolated"):
            assert field in leg
        assert leg["notional_usd"] == 10
    assert payload["legs"][0]["direction"] == "SELL"
    assert payload["legs"][0]["isolated"] is True
    assert payload["legs"][1]["direction"] == "BUY"
    assert payload["legs"][1]["available"] == 9.0
    # Accrual is not folded into the net edge.
    assert payload["net_edge_bps"] != pytest.approx(payload["net_edge_bps"] + payload["accrual_bps"])
    broken = dict(payload)
    del broken["accrual_label"]
    assert "accrual_label" in confirm_field_errors(broken)
    broken = dict(payload)
    broken["accrual_bps"] = 0.054
    assert any("accrual" in err for err in confirm_field_errors(broken))
    broken = dict(payload)
    broken["confirm"] = False
    assert any("confirm" in err for err in confirm_field_errors(broken))


def test_confirm_refused_when_not_armed_and_routes_when_admitted(tmp_path):
    inside = _session(tmp_path / "in", INSIDE)
    _arm(inside)
    spec = inside.task()
    spec["live_armed"] = False
    inside._write_json(inside.task_path, spec)
    payload = build_confirm_payload(
        inside.task(), inside.queue()["pending"], rth=True,
        tail=inside.queue()["pending"]["tail_vs_median"])
    with pytest.raises(ProbeError) as raised:
        inside.admit_confirm(payload)
    assert raised.value.status_code == 409
    assert "live-armed" in str(raised.value)

    routed = _session(tmp_path / "go", INSIDE)
    _arm(routed)
    good = build_confirm_payload(
        routed.task(), routed.queue()["pending"], rth=True,
        tail=routed.queue()["pending"]["tail_vs_median"])
    admitted = routed.admit_confirm(good)
    assert admitted["sent"] is True
    assert admitted["routed"] is True
    assert admitted["confirm_id"]
    assert admitted["force_confirm_outside_rth"] is False
    assert routed.queue()["pending"] is None
    assert routed.queue()["history"][-1]["action"] == "confirmed"
    assert routed.queue()["history"][-1]["sent"] is True
    log = (routed.log_path).read_text(encoding="utf-8")
    assert "force_confirm_outside_rth=false" in log
    assert "test-key" not in log


def test_default_start_argv_is_record_only(tmp_path, monkeypatch):
    sess = _session(tmp_path, INSIDE, env=True)
    sess.save_task({"mode": "live", "manual_confirm": True, "rth_only": True})
    captured = {}

    def fake_start(symbol, hedge):
        argv = list(sess.ctl.command_builder(symbol, hedge))
        captured["argv"] = argv
        return {"pid": None, "argv": argv, "symbol": symbol, "hedge": hedge,
                "running": True}

    monkeypatch.setattr(sess.ctl, "start", fake_start)
    info = sess.start()
    assert info["live_armed"] is True
    assert info["record_only"] is True
    argv = captured["argv"]
    assert "--record-only" in argv
    assert "--no-dashboard" in argv
    assert argv[argv.index("--symbol") + 1] == "SNDK"
    assert argv[argv.index("--hedge") + 1] == "lighter"
    assert ".env" not in argv
    joined = " ".join(argv)
    assert "--record-only" in joined
    assert "LIVE" not in joined


def test_live_arm_blocked_outside_rth_and_without_confirm(tmp_path):
    sess = _session(tmp_path, OUTSIDE)
    sess.save_task({"mode": "live", "manual_confirm": True, "rth_only": True})
    with pytest.raises(ProbeError) as raised:
        sess.start()
    assert raised.value.status_code == 409
    assert "RTH" in str(raised.value)

    off = _session(tmp_path / "off", INSIDE)
    off.save_task({"mode": "live", "manual_confirm": False, "rth_only": True})
    with pytest.raises(ProbeError) as raised:
        off.start()
    assert DECISION_WARNING in str(raised.value)
    assert raised.value.status_code == 409


def test_missing_env_refuses_live_and_confirm(tmp_path):
    calls = []
    sess = _session(tmp_path, INSIDE, env=False, calls=calls)
    sess.save_task({"mode": "live", "manual_confirm": True, "rth_only": True})
    with pytest.raises(ProbeError) as raised:
        sess.start()
    assert raised.value.status_code == 409
    assert "HL_ACCOUNT_ADDRESS" in str(raised.value)
    assert "LIGHTER_API_PRIVATE_KEY" in str(raised.value)
    assert calls == []

    _write_env(tmp_path, complete=False)
    _arm(sess)
    payload = build_confirm_payload(
        sess.task(), sess.queue()["pending"], rth=True,
        tail=sess.queue()["pending"]["tail_vs_median"])
    with pytest.raises(ProbeError) as raised:
        sess.admit_confirm(payload)
    assert raised.value.status_code == 409
    assert "密钥" in str(raised.value)
    assert calls == []
    assert sess.queue()["pending"] is not None


def test_outside_rth_requires_second_force_confirm(tmp_path):
    calls = []
    sess = _session(tmp_path, OUTSIDE, calls=calls)
    _arm(sess)
    snap = session_snapshot(datetime.fromtimestamp(OUTSIDE, UTC), -1.7)
    assert snap["name"] == "us_post_overnight"
    assert snap["midline_bps"] == 2.6
    assert snap["task_midline_bps"] == -1.7
    assert snap["deviation_text"] == "偏离约 4 bps"
    assert snap["risk_line"] == FORCE_RISK_LINE
    # The task midline stays the user's number. The session does not rewrite it.
    assert sess.task()["midline_bps"] == -1.7
    text = sess.config_path.read_text(encoding="utf-8")
    assert "midline_bps: -1.7" in text

    payload = build_confirm_payload(
        sess.task(), sess.queue()["pending"], rth=False,
        tail=sess.queue()["pending"]["tail_vs_median"])
    first = sess.admit_confirm(payload)
    assert first["needs_force_confirm"] is True
    assert first["routed"] is False
    assert first["sent"] is False
    assert calls == []
    card = first["force_card"]
    assert card["name"] == "us_post_overnight"
    assert card["midline_bps"] == 2.6
    assert card["task_midline_bps"] == -1.7
    assert card["deviation_text"] == "偏离约 4 bps"
    assert card["risk_line"] == "带宽 1 bps，错中枢风险大于费率缺口"
    assert sess.queue()["pending"]["force_ack"] is True

    jumped = dict(payload)
    jumped["force_confirm"] = True
    # A fresh session without the ack must refuse even with the flag set.
    other_calls = []
    other = _session(tmp_path / "jump", OUTSIDE, calls=other_calls)
    _arm(other)
    jumped_other = build_confirm_payload(
        other.task(), other.queue()["pending"], rth=False,
        tail=other.queue()["pending"]["tail_vs_median"])
    jumped_other["force_confirm"] = True
    with pytest.raises(ProbeError) as raised:
        other.admit_confirm(jumped_other)
    assert raised.value.status_code == 409
    assert other_calls == []

    second = sess.admit_confirm(dict(payload, force_confirm=True))
    assert second["sent"] is True
    assert second["routed"] is True
    assert second["force_confirm_outside_rth"] is True
    assert len(calls) == 1
    assert calls[0]["confirm_id"]
    log = sess.log_path.read_text(encoding="utf-8")
    assert "force_confirm_outside_rth=true" in log
    assert "test-key" not in log


def test_single_leg_halts_and_blocks_new_opens(tmp_path):
    calls = []

    def one_leg(req):
        calls.append(req)
        return _flat_result(req, buy=0.05, sell=0.0, fee=0.9)

    sess = _session(tmp_path, INSIDE, executor=one_leg)
    _arm(sess)
    payload = build_confirm_payload(
        sess.task(), sess.queue()["pending"], rth=True,
        tail=sess.queue()["pending"]["tail_vs_median"])
    body = sess.admit_confirm(payload)
    assert body["sent"] is True
    assert body["halted"] is True
    status = sess.overlay({"running": True, "paused": False, "latest": None,
                            "warnings": []})
    assert status["status_label"] == "HALT"
    assert status["halted"] is True
    assert abs(status["net_base"]) > 0
    _arm(sess)
    again = build_confirm_payload(
        sess.task(), sess.queue()["pending"], rth=True,
        tail=sess.queue()["pending"]["tail_vs_median"])
    with pytest.raises(ProbeError) as raised:
        sess.admit_confirm(again)
    assert raised.value.status_code == 409
    assert "HALT" in str(raised.value)
    assert len(calls) == 1


def test_manual_confirm_required_and_no_silent_fire(tmp_path):
    calls = []
    off = _session(tmp_path, INSIDE, calls=calls)
    off.save_task({"mode": "live", "manual_confirm": False, "rth_only": True})
    assert off.task()["warnings"] == [DECISION_WARNING]
    assert off.task()["live_armed"] is False
    with pytest.raises(ProbeError) as raised:
        off.start()
    assert DECISION_WARNING in str(raised.value)
    assert calls == []

    shifted = _session(tmp_path / "mid", INSIDE, calls=calls)
    saved = shifted.save_task({
        "mode": "live", "manual_confirm": True, "midline_bps": 0,
    })
    assert saved["warnings"] == [DECISION_WARNING]
    assert saved["live_armed"] is False
    assert calls == []
    # Creating a task never arms live and never sends.
    plain = _session(tmp_path / "rec", INSIDE, calls=calls)
    created = plain.save_task({})
    assert created["mode"] == "record"
    assert created["live_armed"] is False
    assert calls == []


def test_fee_mismatch_stops_further_confirms(tmp_path):
    calls = []

    def pricey(req):
        calls.append(req)
        return _flat_result(req, fee=1.5)

    sess = _session(tmp_path, INSIDE, executor=pricey)
    _arm(sess)
    payload = build_confirm_payload(
        sess.task(), sess.queue()["pending"], rth=True,
        tail=sess.queue()["pending"]["tail_vs_median"])
    body = sess.admit_confirm(payload)
    assert body["sent"] is True
    assert body["fee_mismatch"] is True
    status = sess.overlay({"running": True, "paused": False, "latest": {
        "minute_ts": INSIDE, "samples": 60,
    }, "warnings": []})
    assert status["fee_check"]["assumed_bps"] == 0.9
    assert status["fee_check"]["actual_bps"] == pytest.approx(1.5)
    assert status["fee_check"]["mismatch"] is True
    _arm(sess)
    again = build_confirm_payload(
        sess.task(), sess.queue()["pending"], rth=True,
        tail=sess.queue()["pending"]["tail_vs_median"])
    with pytest.raises(ProbeError) as raised:
        sess.admit_confirm(again)
    assert "0.9" in str(raised.value)
    assert len(calls) == 1


def test_route_refuses_without_confirm_id():
    with pytest.raises(RuntimeError, match="confirm id"):
        execute_admitted({})


def test_record_argv_stays_record_only_and_probe_yaml_loads(tmp_path):
    sess = _session(tmp_path, INSIDE)
    saved = sess.save_task({})
    assert saved["warnings"] == []
    assert saved["mode_label"] == "只记录"
    argv = sess.record_argv("SNDK", "lighter")
    assert "--record-only" in argv
    assert "--config" in argv
    assert ".env" not in argv
    assert "--env-file" not in argv
    cfg = load_config(str(sess.config_path), NO_ENV,
                      symbol="SNDK", hedge_venue="lighter")
    assert cfg.midline_bps == -1.7
    assert cfg.upper_bps == 1.0 and cfg.lower_bps == 1.0
    assert cfg.max_order_notional == 10
    assert cfg.entropy.cap_usd == 10 and cfg.hedge.cap_usd == 10
    assert cfg.entropy.fee_bps == 0.9 and cfg.hedge.fee_bps == 0.0
    assert cfg.referral_mode == "self_t2"
    assert cfg.premium_persist_sec == 3.0
    assert cfg.rebate_accrual_only is True


def test_accounts_do_not_leak_secrets(tmp_path):
    secret = "0x" + "ab" * 32
    (tmp_path / ".env").write_text(
        "HL_PRIVATE_KEY=" + secret + "\n"
        "HL_ACCOUNT_ADDRESS=0x" + "cd" * 20 + "\n"
        "LIGHTER_ACCOUNT_INDEX=7\n"
        "LIGHTER_API_KEY_INDEX=3\n"
        "LIGHTER_API_PRIVATE_KEY=" + secret + "\n",
        encoding="utf-8")

    class Resp:
        def __init__(self, payload):
            self.payload = payload
        def read(self):
            return json.dumps(self.payload).encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def opener(req, timeout=None):
        url = req.full_url
        if "hyperliquid" in url:
            return Resp({
                "marginSummary": {"accountValue": "39.5"},
                "withdrawable": "20.0",
                "assetPositions": [{
                    "position": {
                        "coin": "io:SNDK",
                        "szi": "0.1",
                        "leverage": {"type": "isolated", "value": 5},
                    }
                }],
            })
        return Resp({
            "accounts": [{
                "total_asset_value": "40.0",
                "available_balance": "15.5",
                "positions": [{
                    "symbol": "SNDK",
                    "sign": 1,
                    "position": "0.2",
                    "margin_mode": "isolated",
                }],
            }]
        })

    snap = read_accounts(tmp_path, opener=opener)
    blob = json.dumps(snap)
    assert secret not in blob
    assert "PRIVATE" not in blob
    assert snap["entropy"]["equity"] == pytest.approx(39.5)
    assert snap["entropy"]["available"] == pytest.approx(20.0)
    assert snap["entropy"]["position"] == pytest.approx(0.1)
    assert snap["entropy"]["isolated"] is True
    assert snap["lighter"]["equity"] == pytest.approx(40.0)
    assert snap["lighter"]["isolated"] is True
    leaked = scrub({"note": "key " + secret})
    assert secret not in json.dumps(leaked)
    assert leaked["note"] == "[redacted]"


def test_http_confirm_gate(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from web.panel import create_app

    clock = {"t": INSIDE}

    def _sleep(symbol, hedge):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    calls = []

    def executor(req):
        calls.append(req)
        assert req.get("confirm_id")
        return _flat_result(req)

    _write_env(tmp_path)
    app = create_app(tmp_path, command_builder=_sleep,
                     now=lambda: clock["t"],
                     executor=executor,
                     account_reader=lambda: {
                         "creds": {"entropy": True, "lighter": True},
                         "entropy": {"equity": 10, "available": 8,
                                     "position": 0, "isolated": True},
                         "lighter": {"equity": 12, "available": 9,
                                     "position": 0, "isolated": False},
                         "note": None,
                     })
    # A qualifying sell: pre-fee 8 → net 7.1, hurdle is -1.7+1.0 = -0.7.
    logs = tmp_path / "logs"
    logs.mkdir()
    header = ("minute_ts,time_utc,entropy_bid,entropy_ask,hedge_bid,hedge_ask,"
              "premium_close_bps,sell_edge_mean_bps,buy_edge_mean_bps,samples,"
              "fill_sell_edge_100_bps,fill_buy_edge_100_bps\n")
    row = f"{int(INSIDE)},2026-01-07T15:00:00Z,100,100.1,100,100.1,1.0,8,1,60,8,1\n"
    (logs / "minutes.csv").write_text(header + row, encoding="utf-8")

    with TestClient(app) as client:
        made = client.post("/api/task", json={
            "mode": "live", "manual_confirm": True, "rth_only": True,
            "midline_bps": -1.7, "upper_bps": 1, "lower_bps": 1,
            "order_notional_usd": 10, "max_position_usd": 10,
        })
        assert made.status_code == 200, made.text
        assert made.json()["warnings"] == []
        drifted = client.post("/api/task", json={
            "mode": "live", "manual_confirm": False, "rth_only": True,
            "midline_bps": 0, "upper_bps": 1, "lower_bps": 1,
            "order_notional_usd": 10, "max_position_usd": 10,
        })
        assert drifted.status_code == 200
        assert drifted.json()["warnings"] == ["会偏离 Decision Card"]
        # Put the card back on the Decision Card before arming.
        made = client.post("/api/task", json={
            "mode": "live", "manual_confirm": True, "rth_only": True,
            "midline_bps": -1.7, "upper_bps": 1, "lower_bps": 1,
            "order_notional_usd": 10, "max_position_usd": 10,
        })
        assert made.status_code == 200
        started = client.post("/api/session/start")
        assert started.status_code == 200, started.text
        assert started.json()["live_armed"] is True
        assert started.json()["record_only"] is True
        status = client.get("/api/status").json()
        payload = status["proposal"]
        assert payload["confirm_enabled"] is True
        assert payload["accrual_label"] == "未到账"
        assert payload["accrual_bps"] == pytest.approx(0.54)
        assert payload["fee_bps"] == 0.9
        assert payload["rth"] is True
        assert payload["midline_bps"] == -1.7
        assert "HL_PRIVATE" not in json.dumps(status)
        missing = dict(payload)
        del missing["accrual_label"]
        bad = client.post("/api/confirm", json=missing)
        assert bad.status_code == 400
        clock["t"] = OUTSIDE
        payload["rth"] = False
        held = client.post("/api/confirm", json=payload)
        assert held.status_code == 200, held.text
        assert held.json()["needs_force_confirm"] is True
        assert held.json()["sent"] is False
        assert calls == []
        card = held.json()["force_card"]
        assert card["risk_line"] == "带宽 1 bps，错中枢风险大于费率缺口"
        assert card["name"] == "us_post_overnight"
        assert card["deviation_text"] == "偏离约 4 bps"
        clock["t"] = INSIDE
        # The pending net may have been refreshed; re-read so the echo matches.
        payload = client.get("/api/status").json()["proposal"]
        assert payload["confirm_enabled"] is True
        assert payload["rth"] is True
        ok = client.post("/api/confirm", json=payload)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["routed"] is True
        assert body["sent"] is True
        assert body["confirm_id"]
        assert len(calls) == 1
        again = client.get("/api/status").json()["proposal"]
        assert again is not None
        skipped = client.post("/api/confirm/cancel")
        assert skipped.status_code == 200
        assert skipped.json()["skipped"] is True
        assert skipped.json()["sent"] is False
        client.post("/api/stop")


def test_pause_resume_and_reconcile_do_not_route(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from web.panel import create_app

    def _sleep(symbol, hedge):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    app = create_app(tmp_path, command_builder=_sleep, now=lambda: INSIDE,
                     account_reader=lambda: {"creds": {"entropy": False, "lighter": False},
                                             "entropy": {}, "lighter": {}, "note": "未配置密钥"})
    with TestClient(app) as client:
        assert client.post("/api/task", json={"mode": "record"}).status_code == 200
        started = client.post("/api/session/start")
        assert started.status_code == 200, started.text
        assert started.json()["record_only"] is True
        paused = client.post("/api/pause")
        assert paused.status_code == 200, paused.text
        assert paused.json()["paused"] is True
        status = client.get("/api/status").json()
        assert status["status_label"] == "暂停"
        assert status["proposal"] is None
        recon = client.post("/api/reconcile")
        assert recon.status_code == 200
        assert recon.json()["routed"] is False
        resumed = client.post("/api/session/start")
        assert resumed.status_code == 200
        assert resumed.json()["resumed"] is True
        client.post("/api/stop")
        status = client.get("/api/status").json()
        assert status["running"] is False
        assert status["status_label"] == "已停止"
