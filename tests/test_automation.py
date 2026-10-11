"""Balance cap and session-midline switch on top of the later auto gates.

Run:  python3 -m pytest tests/test_automation.py
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from web.accounts import read_accounts  # noqa: E402
from web.probe import (  # noqa: E402
    available_short_reason,
    dynamic_balance_cap,
    notional_cap_reason,
    session_switch_decision,
)
from web.recorder_ctl import RecorderControl  # noqa: E402
from web.session import ProbeError, ProbeSession  # noqa: E402

NY = ZoneInfo("America/New_York")


def _ts(moment: datetime) -> float:
    return moment.timestamp()


INSIDE = _ts(datetime(2026, 1, 7, 10, 0, tzinfo=NY))
OUTSIDE = _ts(datetime(2026, 1, 7, 16, 0, tzinfo=NY))


def _accounts(available_e=100.0, available_l=80.0, pos_e=0.0, pos_l=0.0):
    return {
        "creds": {"entropy": True, "lighter": True},
        "entropy": {"equity": available_e, "available": available_e,
                    "position": pos_e, "isolated": True},
        "lighter": {"equity": available_l, "available": available_l,
                    "position": pos_l, "isolated": False},
        "note": None,
    }


def _session(tmp_path, clock, *, accounts):
    (tmp_path / ".env").write_text(
        "HL_PRIVATE_KEY=test-key\n"
        "HL_ACCOUNT_ADDRESS=0xabc\n"
        "LIGHTER_ACCOUNT_INDEX=1\n"
        "LIGHTER_API_KEY_INDEX=2\n"
        "LIGHTER_API_PRIVATE_KEY=test-lighter\n",
        encoding="utf-8")

    sess = ProbeSession(
        tmp_path, RecorderControl(tmp_path), now=lambda: clock["t"],
        account_reader=lambda: accounts(),
        executor=lambda req: {
            "ok": True, "routed": True, "sent": True, "halted": False,
            "confirm_id": req.get("confirm_id"), "buy_fill": 0.01,
            "sell_fill": 0.01, "net_base": 0.0, "entropy_fee_bps": 0.9,
            "status": "filled/filled", "error": None,
        })
    sess.save_task({
        "mode": "live", "manual_confirm": True, "rth_only": True,
        "order_notional_usd": 11, "max_position_usd": 11,
    })
    return sess


def _arm(sess, *, anchor="us_regular"):
    sess.save_task({
        "mode": "live", "manual_confirm": True, "rth_only": True,
        "midline_bps": -1.7, "upper_bps": 1, "lower_bps": 1,
        "order_notional_usd": 11, "max_position_usd": 11,
    })
    spec = sess.task()
    spec["live_armed"] = True
    spec["session_anchor"] = anchor
    sess._write_json(sess.task_path, spec)
    return spec


def _overlay(sess, ts):
    return sess.overlay({
        "running": True, "paused": False,
        "latest": {
            "minute_ts": ts, "samples": 60,
            "tob": {"sell_edge_mean_bps": 8, "buy_edge_mean_bps": 1,
                    "premium_close_bps": 1},
            "fillable_100": {"sell_bps": 8, "buy_bps": 1},
        },
        "warnings": [],
    })


def test_dynamic_cap_uses_smaller_available_and_safety():
    both = _accounts(20, 30)
    assert dynamic_balance_cap(both) == pytest.approx(18.0)
    assert dynamic_balance_cap(both, safety=0.5) == pytest.approx(10.0)
    assert dynamic_balance_cap(_accounts(100, 12)) == pytest.approx(10.8)
    missing = _accounts()
    missing["lighter"]["available"] = None
    assert dynamic_balance_cap(missing) is None
    assert dynamic_balance_cap({"entropy": {}, "lighter": {"available": 50}}) is None
    assert notional_cap_reason(11, 11, both) is None
    assert "动态上限" in notional_cap_reason(19, 19, both)
    assert "10.5" in notional_cap_reason(10, 10, both)
    assert available_short_reason(_accounts(9, 40), 11).startswith("可用不足")
    assert "不猜测" in available_short_reason(missing, 11)


def test_save_rejects_notional_above_live_cap(tmp_path):
    state = {"e": 20.0, "l": 20.0}

    def accounts():
        return _accounts(state["e"], state["l"])

    clock = {"t": INSIDE}
    sess = _session(tmp_path, clock, accounts=accounts)
    with pytest.raises(ProbeError) as raised:
        sess.save_task({
            "mode": "live", "manual_confirm": True,
            "order_notional_usd": 19, "max_position_usd": 19,
        })
    assert raised.value.status_code == 400
    assert "动态上限" in str(raised.value)
    saved = sess.save_task({
        "mode": "record", "order_notional_usd": 18, "max_position_usd": 18,
    })
    assert saved["order_notional_usd"] == 18
    view = sess.overlay({"running": False, "paused": False, "latest": None,
                          "warnings": []})
    assert view["balance"]["dynamic_cap_usd"] == pytest.approx(18.0)


def test_cash_confirm_blocked_when_available_is_short(tmp_path):
    def accounts():
        return _accounts(0, 100)

    clock = {"t": INSIDE}
    (tmp_path / ".env").write_text(
        "HL_PRIVATE_KEY=test-key\nHL_ACCOUNT_ADDRESS=0xabc\n"
        "LIGHTER_ACCOUNT_INDEX=1\nLIGHTER_API_KEY_INDEX=2\n"
        "LIGHTER_API_PRIVATE_KEY=test-lighter\n",
        encoding="utf-8")
    sess = ProbeSession(
        tmp_path, RecorderControl(tmp_path), now=lambda: clock["t"],
        account_reader=lambda: accounts())
    with pytest.raises(ProbeError) as raised:
        sess.save_task({
            "mode": "live", "manual_confirm": True,
            "order_notional_usd": 11, "max_position_usd": 11,
        })
    assert "可用不足" in str(raised.value)


def test_unified_spot_usdc_is_the_entropy_available(tmp_path):
    address = "0x" + "11" * 20
    (tmp_path / ".env").write_text(
        "HL_PRIVATE_KEY=test-key-not-a-hex-key\n"
        "HL_ACCOUNT_ADDRESS=" + address + "\n",
        encoding="utf-8")

    def opener(req, timeout=None):
        body = json.loads(req.data.decode())

        class Resp:
            def read(self):
                if body["type"] == "clearinghouseState":
                    payload = {
                        "marginSummary": {"accountValue": "0.0"},
                        "withdrawable": "0",
                        "assetPositions": [{
                            "position": {
                                "coin": "io:SNDK", "szi": "0",
                                "leverage": {"type": "isolated"},
                            }
                        }],
                    }
                else:
                    payload = {
                        "balances": [{
                            "coin": "USDC", "token": 0, "total": "40",
                            "hold": "0",
                        }],
                        "tokenToAvailableAfterMaintenance": [["USDC", "28.5"]],
                    }
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Resp()

    snap = read_accounts(tmp_path, opener=opener)
    assert address not in json.dumps(snap)
    assert snap["entropy"]["available"] == pytest.approx(28.5)
    assert snap["entropy"]["equity"] == pytest.approx(40.0)
    assert dynamic_balance_cap({
        "entropy": snap["entropy"],
        "lighter": {"available": 50, "position": 0},
    }) == pytest.approx(28.5 * 0.9)


def test_session_switch_blocked_when_not_flat(tmp_path):
    book = {"e": 0.4, "l": -0.2}

    def accounts():
        return _accounts(pos_e=book["e"], pos_l=book["l"])

    clock = {"t": INSIDE}
    sess = _session(tmp_path, clock, accounts=accounts)
    _arm(sess)
    clock["t"] = OUTSIDE
    status = _overlay(sess, OUTSIDE)
    assert sess.task()["midline_bps"] == -1.7
    assert sess.task()["upper_bps"] == 1.0
    assert "未同时空仓" in status["session_switch_block"]
    assert status["proposal"] is None or status["proposal"]["confirm_enabled"] is False
    log = sess.log_path.read_text(encoding="utf-8")
    _overlay(sess, OUTSIDE)
    assert sess.log_path.read_text(encoding="utf-8") == log
    assert log.count("session switch blocked") == 1


def test_session_switch_applied_when_flat(tmp_path):
    def accounts():
        return _accounts()

    clock = {"t": INSIDE}
    sess = _session(tmp_path, clock, accounts=accounts)
    _arm(sess)
    clock["t"] = OUTSIDE
    status = _overlay(sess, OUTSIDE)
    task = sess.task()
    assert task["midline_bps"] == pytest.approx(2.6)
    assert task["upper_bps"] == 1.0
    assert task["session_anchor"] == "us_post_overnight"
    assert status["session_switch_block"] in (None, "")
    log = sess.log_path.read_text(encoding="utf-8")
    assert "session switch us_regular -> us_post_overnight midline -1.7 -> 2.6" in log
    _overlay(sess, OUTSIDE)
    assert sess.log_path.read_text(encoding="utf-8").count(
        "session switch us_regular") == 1


def test_session_switch_does_not_replace_rth_start_gate(tmp_path):
    def accounts():
        return _accounts()

    clock = {"t": OUTSIDE}
    sess = _session(tmp_path, clock, accounts=accounts)
    sess.save_task({
        "mode": "live", "manual_confirm": True, "rth_only": True,
        "order_notional_usd": 11, "max_position_usd": 11,
    })
    first = sess.start()
    assert first["needs_force_start"] is True
    assert sess.task().get("live_armed") is not True


def test_session_switch_decision_unknown_position_does_not_apply():
    decision = session_switch_decision(
        "us_regular", "us_post_overnight",
        {"entropy": {"position": None}, "lighter": {"position": 0}},
        armed=True)
    assert decision["action"] == "block"
    flat = session_switch_decision(
        "us_regular", "asia",
        {"entropy": {"position": 0}, "lighter": {"position": 0.0}},
        armed=True)
    assert flat["action"] == "apply"
    assert flat["midline_bps"] == pytest.approx(-0.4)
