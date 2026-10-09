"""Balance cap, session-midline switch, and auto-confirm.

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
    auto_confirm_decision,
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


INSIDE = _ts(datetime(2026, 1, 7, 10, 0, tzinfo=NY))   # us_regular
OUTSIDE = _ts(datetime(2026, 1, 7, 16, 0, tzinfo=NY))  # us_post_overnight


def _flat_result(req):
    return {
        "ok": True, "routed": True, "sent": True, "halted": False,
        "confirm_id": req["confirm_id"], "buy_fill": 0.01, "sell_fill": 0.01,
        "net_base": 0.0, "entropy_fee_bps": 0.9, "status": "filled/filled",
        "error": None,
    }


def _accounts(available_e=100.0, available_l=80.0, pos_e=0.0, pos_l=0.0):
    return {
        "creds": {"entropy": True, "lighter": True},
        "entropy": {"equity": available_e, "available": available_e,
                    "position": pos_e, "isolated": True},
        "lighter": {"equity": available_l, "available": available_l,
                    "position": pos_l, "isolated": False},
        "note": None,
    }


def _session(tmp_path, clock, *, accounts, calls=None, body=None):
    (tmp_path / ".env").write_text(
        "HL_PRIVATE_KEY=test-key\n"
        "HL_ACCOUNT_ADDRESS=0xabc\n"
        "LIGHTER_ACCOUNT_INDEX=1\n"
        "LIGHTER_API_KEY_INDEX=2\n"
        "LIGHTER_API_PRIVATE_KEY=test-lighter\n",
        encoding="utf-8")
    box = calls if calls is not None else []

    def _exec(req):
        box.append(req)
        return _flat_result(req)

    sess = ProbeSession(
        tmp_path, RecorderControl(tmp_path), now=lambda: clock["t"],
        account_reader=lambda: accounts(),
        executor=_exec)
    sess.save_task(body or {
        "mode": "live", "manual_confirm": True, "rth_only": True,
        "order_notional_usd": 11, "max_position_usd": 11,
    })
    return sess, box


def _arm(sess, *, anchor="us_regular", auto=False, order=11, auto_max=None,
         sec=3):
    body = {
        "mode": "live", "manual_confirm": True, "rth_only": True,
        "midline_bps": -1.7, "upper_bps": 1, "lower_bps": 1,
        "order_notional_usd": order, "max_position_usd": order,
        "auto_confirm": auto, "auto_confirm_sec": sec,
    }
    if auto_max is not None:
        body["auto_confirm_max_usd"] = auto_max
    sess.save_task(body)
    spec = sess.task()
    spec["live_armed"] = True
    spec["session_anchor"] = anchor
    sess._write_json(sess.task_path, spec)
    return spec


def _latest(ts):
    return {
        "minute_ts": ts,
        "samples": 60,
        "tob": {"sell_edge_mean_bps": 8, "buy_edge_mean_bps": 1,
                "premium_close_bps": 1},
        "fillable_100": {"sell_bps": 8, "buy_bps": 1},
    }


def _overlay(sess, ts):
    return sess.overlay({
        "running": True, "paused": False, "latest": _latest(ts),
        "warnings": [],
    })


# ---------------------------------------------------------------- balance cap


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
    sess, _calls = _session(tmp_path, clock, accounts=accounts)
    with pytest.raises(ProbeError) as raised:
        sess.save_task({
            "mode": "live", "manual_confirm": True,
            "order_notional_usd": 19, "max_position_usd": 19,
        })
    assert raised.value.status_code == 400
    assert "动态上限" in str(raised.value)
    # 20 * 0.9 = 18, so 18 is inside the band and 10 is under the venue floor.
    saved = sess.save_task({
        "mode": "record", "order_notional_usd": 18, "max_position_usd": 18,
    })
    assert saved["order_notional_usd"] == 18
    view = sess.overlay({"running": False, "paused": False, "latest": None,
                          "warnings": []})
    assert view["balance"]["dynamic_cap_usd"] == pytest.approx(18.0)
    assert view["balance"]["entropy_available"] == pytest.approx(20.0)
    assert view["balance"]["lighter_available"] == pytest.approx(20.0)


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
                            "available": "30",
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


# --------------------------------------------------------------- session switch


def test_session_switch_blocked_when_not_flat(tmp_path):
    book = {"e": 0.4, "l": -0.2}

    def accounts():
        return _accounts(pos_e=book["e"], pos_l=book["l"])

    clock = {"t": INSIDE}
    sess, _calls = _session(tmp_path, clock, accounts=accounts)
    _arm(sess)
    clock["t"] = OUTSIDE
    status = _overlay(sess, OUTSIDE)
    assert sess.task()["midline_bps"] == -1.7
    assert sess.task()["upper_bps"] == 1.0
    assert sess.task()["rth_only"] is True
    assert "未同时空仓" in status["session_switch_block"]
    assert status["proposal"] is None or status["proposal"]["confirm_enabled"] is False
    log = sess.log_path.read_text(encoding="utf-8")
    _overlay(sess, OUTSIDE)
    again = sess.log_path.read_text(encoding="utf-8")
    assert again == log
    assert log.count("session switch blocked") == 1
    assert "session switch us_regular ->" not in log


def test_session_switch_applied_when_flat_and_not_every_tick(tmp_path):
    def accounts():
        return _accounts()

    clock = {"t": INSIDE}
    sess, _calls = _session(tmp_path, clock, accounts=accounts)
    _arm(sess)
    clock["t"] = OUTSIDE
    status = _overlay(sess, OUTSIDE)
    task = sess.task()
    assert task["midline_bps"] == pytest.approx(2.6)
    assert task["upper_bps"] == 1.0
    assert task["lower_bps"] == 1.0
    assert task["session_anchor"] == "us_post_overnight"
    assert task["rth_only"] is True
    assert status["session_switch_block"] in (None, "")
    log = sess.log_path.read_text(encoding="utf-8")
    assert "session switch us_regular -> us_post_overnight midline -1.7 -> 2.6" in log
    _overlay(sess, OUTSIDE)
    assert sess.log_path.read_text(encoding="utf-8").count(
        "session switch us_regular") == 1
    # Same-session ticks do not move the midline.
    clock["t"] = OUTSIDE + 30
    _overlay(sess, OUTSIDE + 30)
    assert sess.task()["midline_bps"] == pytest.approx(2.6)


def test_session_switch_does_not_replace_rth_start_gate(tmp_path):
    def accounts():
        return _accounts()

    clock = {"t": OUTSIDE}
    sess, _calls = _session(tmp_path, clock, accounts=accounts)
    sess.save_task({
        "mode": "live", "manual_confirm": True, "rth_only": True,
        "order_notional_usd": 11, "max_position_usd": 11,
    })
    with pytest.raises(ProbeError) as raised:
        sess.start()
    assert "RTH" in str(raised.value)
    assert sess.task()["midline_bps"] == -1.7
    assert sess.task().get("live_armed") is False


def test_session_switch_decision_unknown_position_does_not_apply():
    accounts = _accounts()
    accounts["entropy"]["position"] = None
    decision = session_switch_decision(
        "us_regular", "asia", accounts, armed=True)
    assert decision["action"] == "block"
    assert "持仓未读到" in decision["reason"]
    flat = session_switch_decision(
        "us_regular", "asia", _accounts(), armed=True)
    assert flat["action"] == "apply"
    assert flat["midline_bps"] == pytest.approx(-0.4)
    assert session_switch_decision(
        "asia", "asia", _accounts(), armed=True)["action"] == "hold"


# ---------------------------------------------------------------- auto-confirm


def _ready(**overrides):
    base = dict(
        armed=True, halted=False, paused=False, fresh=True, qualifying=True,
        funding_block=None, cap_block=None, order_usd=11, auto_max_usd=11,
        age_sec=3, wait_sec=3, rth=True,
    )
    base.update(overrides)
    return auto_confirm_decision(**base)


def test_auto_confirm_decision_matrix():
    assert _ready() == ("fire", None)
    assert _ready(armed=False)[0] == "off"
    assert _ready(halted=True) == ("refuse", "HALT")
    assert _ready(fresh=False)[1] == "books not fresh"
    assert _ready(qualifying=False)[1] == "not qualifying"
    assert _ready(order_usd=15, auto_max_usd=12)[1] == "notional above auto max"
    assert _ready(funding_block="可用不足")[1] == "可用不足"
    assert _ready(cap_block="名义超过动态上限")[1] == "名义超过动态上限"
    assert _ready(order_usd=10)[1] == "below venue min"
    assert _ready(age_sec=1)[0] == "wait"
    assert _ready(rth=False)[1].startswith("非 RTH")
    assert _ready(paused=True)[1] == "paused"


def test_auto_confirm_fires_only_when_armed_fresh_and_under_cap(tmp_path):
    def accounts():
        return _accounts(100, 100)

    clock = {"t": INSIDE}
    calls = []
    sess, box = _session(tmp_path, clock, accounts=accounts, calls=calls)
    _arm(sess, auto=True)
    first = _overlay(sess, INSIDE)
    assert box == []
    assert first["proposal"] is not None
    assert first["auto_confirm"]["armed"] is True
    assert first["auto_confirm"]["remaining_sec"] == pytest.approx(3, abs=0.05)
    clock["t"] = INSIDE + 3
    second = _overlay(sess, INSIDE)
    assert len(box) == 1
    assert box[0]["confirm_id"]
    assert box[0]["order_notional_usd"] == 11
    assert box[0]["balance_cap_usd"] == pytest.approx(90)
    assert second["proposal"] is None
    assert second["auto_confirm"]["last_action"]["fired"] is True
    assert "已自动确认" in second["auto_confirm"]["last_action"]["text"]


def test_auto_confirm_never_fires_when_halted_stale_or_over_max(tmp_path):
    book = {"e": 0.0, "l": 0.2}

    def accounts():
        return _accounts(100, 100, pos_e=book["e"], pos_l=book["l"])

    clock = {"t": INSIDE}
    calls = []
    sess, box = _session(tmp_path, clock, accounts=accounts, calls=calls)
    _arm(sess, auto=True)
    risk = sess.risk()
    risk["halted"] = True
    risk["halt_reason"] = "single-leg fill"
    risk["net_base"] = 0.2
    risk["halted_at"] = INSIDE
    sess._save_risk(risk)
    sess._account_cache = None
    sess._accounts_at = 0.0
    clock["t"] = INSIDE + 5
    status = _overlay(sess, INSIDE)
    assert box == []
    assert status["status_label"] == "HALT"
    assert status["proposal"] is None

    # A card that is already open still must not auto-fire while halted.
    from web.probe import build_confirm_payload, leg_plan
    spec = sess.task()
    proposal = {
        "proposal_id": "halt-1",
        "direction": "sell_entropy",
        "pre_fee_edge_bps": 8,
        "net_edge_bps": 7.1,
        "legs": leg_plan("sell_entropy", 11, accounts()),
        "tail_vs_median": None,
        "created_at": INSIDE,
    }
    sess._save_queue({"pending": proposal, "history": []})
    payload = build_confirm_payload(spec, proposal, rth=True, tail=None)
    assert sess.maybe_auto_confirm(_latest(INSIDE), payload) is None
    assert box == []
    assert "HALT" in sess.task()["last_auto_action"]["text"]

    # Stale book, not halted, flat again.
    book["l"] = 0.0
    sess._save_risk({
        "halted": False, "halt_reason": None, "net_base": 0.0,
        "actual_fee_bps": None, "assumed_fee_bps": 0.9,
        "fee_mismatch": False, "last_order": None, "halted_at": None,
    })
    sess._account_cache = None
    _arm(sess, auto=True)
    stale = _latest(INSIDE - 1000)
    opened = sess.overlay({
        "running": True, "paused": False, "latest": stale, "warnings": [],
    })
    assert opened["proposal"] is not None
    clock["t"] = INSIDE + 4
    sess.overlay({
        "running": True, "paused": False, "latest": stale, "warnings": [],
    })
    assert box == []
    assert "fresh" in sess.task()["last_auto_action"]["text"]

    # Under the balance cap, above the auto-confirm max.
    _arm(sess, auto=True, order=15, auto_max=12)
    clock["t"] = INSIDE
    _overlay(sess, INSIDE)
    clock["t"] = INSIDE + 4
    _overlay(sess, INSIDE)
    assert box == []
    assert "auto max" in sess.task()["last_auto_action"]["text"]


def test_auto_confirm_stays_off_unless_the_flag_is_set(tmp_path):
    def accounts():
        return _accounts(100, 100)

    clock = {"t": INSIDE}
    calls = []
    sess, box = _session(tmp_path, clock, accounts=accounts, calls=calls)
    _arm(sess, auto=False)
    _overlay(sess, INSIDE)
    clock["t"] = INSIDE + 10
    status = _overlay(sess, INSIDE)
    assert box == []
    assert status["auto_confirm"]["armed"] is False
    assert status["proposal"] is not None
