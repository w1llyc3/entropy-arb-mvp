"""Single SNDK probe task: record, arm, pause, reconcile, confirmed orders.

Arming 探针实盘 never builds a live ``main.py`` command. The subprocess, when
one is started, is record-only. A confirm admits an id and only then calls
``Engine.execute_confirmed`` (see ``web.live_exec``). Outside US RTH the
first click does not send; a second 「强制确认」 is required.

A live symmetric book (Entropy short / Lighter long, or the reverse) is
watched against the task band. When premium is back inside the band,
inclusive of the edges, and stays there for ``persist_sec`` (3s), the
panel proposes a reduce-only close. One leg failing still HALTs.

``auto_confirm`` defaults off. When it is on, an open or a close is sent
only inside RTH and only after the round-trip gate (default 1.8 bps, not
the one-way 0.9). Outside RTH the force click stays human.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from web.accounts import missing_live_env, read_accounts, scrub
from web.live_exec import execute_admitted
from web.probe import (
    ACCRUAL_BPS,
    ACCRUAL_LABEL,
    DECISION_WARNING,
    ENTROPY_FEE_BPS,
    FEE_MISMATCH_TOL_BPS,
    MARGIN_SAFETY,
    MIN_ORDER_USD,
    NET_TOL_BASE,
    PERSIST_SEC,
    RTH_WINDOW,
    STATUS_HALT,
    SYMBOL,
    books_fresh,
    build_confirm_payload,
    confirm_field_errors,
    decision_defaults,
    decision_warnings,
    evaluate_auto_gates,
    in_us_rth,
    leg_plan,
    net_edge_bps,
    normalize_task,
    premium_inside_band,
    probe_config_dict,
    qualifying_direction,
    round_trip_fee_bps,
    session_snapshot,
    size_order_notional,
    status_label,
    symmetric_position,
    tail_vs_median,
    trading_day,
)
from web.recorder_ctl import RecorderControl, RecorderError, record_only_argv
from web.report import read_minutes, recorder_csv_rel

_QUEUE_LIMIT = 20


class ProbeError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProbeSession:
    def __init__(self, root: Path, ctl: RecorderControl,
                 now: Optional[Callable[[], float]] = None,
                 account_reader: Optional[Callable] = None,
                 executor: Optional[Callable] = None) -> None:
        self.root = Path(root)
        self.ctl = ctl
        self.now = now or time.time
        self.account_reader = account_reader or (
            lambda: read_accounts(self.root))
        self.executor = executor or execute_admitted
        self.dir = self.root / ".web"
        self.task_path = self.dir / "task.json"
        self.queue_path = self.dir / "confirm_queue.json"
        self.config_path = self.dir / "probe.yaml"
        self.risk_path = self.dir / "risk.json"
        self.log_path = self.dir / "probe.log"
        self.entry_path = self.dir / "entry.json"
        self.daily_path = self.dir / "auto_daily.json"
        self._account_cache = None
        self._accounts_at = 0.0
        self._revert_since = None
        self._revert_watch = None
        self._sizing_note = None
        self._auto_block_until = 0.0
        self._last_auto = None

    # ----------------------------------------------------------------- files

    def _ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, path: Path) -> Optional[dict]:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_json(self, path: Path, data: dict) -> None:
        self._ensure()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def task(self) -> Optional[dict]:
        return self._read_json(self.task_path)

    def queue(self) -> dict:
        data = self._read_json(self.queue_path) or {}
        history = data.get("history")
        if not isinstance(history, list):
            history = []
        pending = data.get("pending")
        if pending is not None and not isinstance(pending, dict):
            pending = None
        return {"pending": pending, "history": history}

    def _save_queue(self, data: dict) -> None:
        data["history"] = list(data.get("history") or [])[-_QUEUE_LIMIT:]
        self._write_json(self.queue_path, data)

    def _blank_risk(self) -> dict:
        return {
            "halted": False,
            "halt_reason": None,
            "net_base": 0.0,
            "actual_fee_bps": None,
            "assumed_fee_bps": ENTROPY_FEE_BPS,
            "fee_mismatch": False,
            "last_order": None,
        }

    def risk(self) -> dict:
        data = self._read_json(self.risk_path) or {}
        base = self._blank_risk()
        for key in base:
            if key in data:
                base[key] = data[key]
        return base

    def _save_risk(self, risk: dict) -> None:
        self._write_json(self.risk_path, risk)

    def _log(self, message: str) -> None:
        self._ensure()
        stamp = datetime.fromtimestamp(float(self.now()), timezone.utc)
        line = scrub(f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')} {message}")
        if not isinstance(line, str):
            line = str(message)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _creds_missing(self) -> list:
        return missing_live_env(self.root)

    def _account_net(self) -> Optional[float]:
        acct = self._accounts()
        ent = (acct.get("entropy") or {}).get("position")
        lig = (acct.get("lighter") or {}).get("position")
        if isinstance(ent, bool) or isinstance(lig, bool):
            return None
        if isinstance(ent, (int, float)) and isinstance(lig, (int, float)):
            return float(ent) + float(lig)
        return None

    def _block_reason(self) -> Optional[str]:
        risk = self.risk()
        net = float(risk.get("net_base") or 0.0)
        if risk.get("halted") or abs(net) > NET_TOL_BASE:
            return "HALT：净敞口不为 0，拒绝新开仓"
        if risk.get("fee_mismatch"):
            return "实际费率与假设 0.9 bps 不一致，已停止确认"
        if self._creds_missing():
            return ("实盘密钥不完整，拒绝确认（需要 HL_PRIVATE_KEY、"
                    "HL_ACCOUNT_ADDRESS、LIGHTER_ACCOUNT_INDEX、"
                    "LIGHTER_API_KEY_INDEX、LIGHTER_API_PRIVATE_KEY）")
        acct_net = self._account_net()
        if acct_net is not None and abs(acct_net) > NET_TOL_BASE:
            return "HALT：账户净敞口不为 0，拒绝新开仓"
        return None

    def _clock(self) -> datetime:
        return datetime.fromtimestamp(float(self.now()), timezone.utc)

    def rth_now(self) -> bool:
        return in_us_rth(self._clock())

    # ------------------------------------------------------------------- task

    def defaults(self) -> dict:
        return decision_defaults()

    def save_task(self, body: dict) -> dict:
        proc = self.ctl.snapshot()
        if proc.get("running"):
            raise ProbeError("stop the probe before changing the task",
                             status_code=409)
        try:
            spec = normalize_task(body)
        except ValueError as exc:
            raise ProbeError(str(exc), status_code=400)
        spec["live_armed"] = False
        spec["warnings"] = decision_warnings(spec)
        spec["saved_at"] = self.now()
        self._write_json(self.task_path, spec)
        self._write_probe_yaml(spec)
        self._save_queue({"pending": None, "history": self.queue()["history"]})
        return self._public_task(spec)

    def clear_task(self) -> dict:
        proc = self.ctl.snapshot()
        if proc.get("running"):
            raise ProbeError("stop the probe before clearing the task",
                             status_code=409)
        for path in (self.task_path,):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self._save_queue({"pending": None, "history": []})
        return {"cleared": True}

    def _write_probe_yaml(self, spec: dict) -> None:
        self._ensure()
        tmp = self.config_path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(probe_config_dict(spec), sort_keys=False),
            encoding="utf-8")
        os.replace(tmp, self.config_path)

    def _public_task(self, spec: Optional[dict]) -> Optional[dict]:
        if not spec:
            return None
        out = {
            "symbol": spec.get("symbol"),
            "entropy_dex": spec.get("entropy_dex"),
            "hedge": spec.get("hedge"),
            "hedge_label": "Lighter",
            "midline_bps": spec.get("midline_bps"),
            "upper_bps": spec.get("upper_bps"),
            "lower_bps": spec.get("lower_bps"),
            "order_notional_usd": spec.get("order_notional_usd"),
            "max_position_usd": spec.get("max_position_usd"),
            "entropy_fee_bps": ENTROPY_FEE_BPS,
            "lighter_fee_bps": 0.0,
            "referral_mode": spec.get("referral_mode"),
            "accrual_bps": ACCRUAL_BPS,
            "accrual_label": ACCRUAL_LABEL,
            "manual_confirm": bool(spec.get("manual_confirm")),
            "auto_confirm": bool(spec.get("auto_confirm")),
            "auto_confirm_max_usd": spec.get("auto_confirm_max_usd"),
            "auto_daily_max_notional_usd": spec.get("auto_daily_max_notional_usd"),
            "auto_daily_max_count": spec.get("auto_daily_max_count"),
            "sizing_mode": spec.get("sizing_mode") or "cash",
            "margin_safety": spec.get("margin_safety"),
            "persist_sec": spec.get("persist_sec") or PERSIST_SEC,
            "rth_only": bool(spec.get("rth_only", True)),
            "mode": spec.get("mode"),
            "mode_label": "探针实盘" if spec.get("mode") == "live" else "只记录",
            "live_armed": bool(spec.get("live_armed")),
            "warnings": list(spec.get("warnings") or decision_warnings(spec)),
        }
        return out

    def _load_task(self) -> dict:
        spec = self.task()
        if not spec:
            raise ProbeError("create the SNDK task first", status_code=409)
        return spec

    def _mark_armed(self, spec: dict, armed: bool) -> dict:
        spec["live_armed"] = bool(armed and spec.get("mode") == "live")
        spec["warnings"] = decision_warnings(spec)
        self._write_json(self.task_path, spec)
        return spec

    # ------------------------------------------------------------------ spawn

    def record_argv(self, symbol: str, hedge: str) -> list:
        """Credential-free collection. Always record-only. Never passes ``.env``."""
        argv = [
            os.environ.get("PYTHON", "") or _python(),
            "main.py",
            "--record-only",
            "--no-dashboard",
            "--symbol", symbol,
            "--hedge", hedge,
            "--config", ".web/probe.yaml",
        ]
        if "--record-only" not in argv:
            raise ProbeError("refusing to start without --record-only",
                             status_code=500)
        if any(part == ".env" or str(part).startswith("--env") for part in argv):
            raise ProbeError("refusing to pass an env file to the browser process",
                             status_code=500)
        return argv

    def _spawn(self, spec: dict) -> dict:
        self._write_probe_yaml(spec)
        symbol, hedge = spec["symbol"], spec["hedge"]
        builder = self.ctl.command_builder
        if builder is record_only_argv or builder is self.record_argv:
            self.ctl.command_builder = self.record_argv
            try:
                info = self.ctl.start(symbol, hedge)
            except RecorderError:
                self.ctl.command_builder = record_only_argv
                raise
            argv = info.get("argv") or []
            if "--record-only" not in argv:
                self.ctl.stop()
                raise ProbeError("refusing to leave a process up without --record-only",
                                 status_code=500)
            return info
        return self.ctl.start(symbol, hedge)

    def start(self) -> dict:
        """启动. Record mode collects. Live mode arms the queue and still records.

        A paused process is continued. Live arm is refused outside RTH when
        the RTH window is on, and refused when manual confirm is off.
        """
        spec = self._load_task()
        proc = self.ctl.snapshot()
        if proc.get("running") and proc.get("paused"):
            try:
                self.ctl.resume()
            except RecorderError as exc:
                raise ProbeError(str(exc), status_code=exc.status_code)
            return {"resumed": True, "live_armed": bool(spec.get("live_armed"))}
        if proc.get("running"):
            raise ProbeError("probe is already running", status_code=409)
        if spec.get("mode") == "live":
            if not spec.get("manual_confirm"):
                raise ProbeError(
                    DECISION_WARNING + "；拒绝无人值守实盘（需要人工确认）",
                    status_code=409)
            if spec.get("rth_only", True) and not self.rth_now():
                raise ProbeError(
                    "非美股 RTH（America/New_York 09:30–16:00），禁止武装探针实盘",
                    status_code=409)
            missing = self._creds_missing()
            if missing:
                raise ProbeError(
                    "实盘密钥不完整，拒绝 LIVE：" + ", ".join(missing),
                    status_code=409)
        try:
            info = self._spawn(spec)
        except RecorderError as exc:
            raise ProbeError(str(exc), status_code=exc.status_code)
        armed = spec.get("mode") == "live"
        self._mark_armed(spec, armed)
        return {
            "running": True,
            "live_armed": armed,
            "mode": spec.get("mode"),
            "pid": info.get("pid"),
            "argv": info.get("argv"),
            "record_only": True,
        }

    def pause(self) -> dict:
        try:
            return self.ctl.pause()
        except RecorderError as exc:
            raise ProbeError(str(exc), status_code=exc.status_code)

    def stop(self) -> dict:
        stopped = self.ctl.stop()
        spec = self.task()
        if spec:
            self._mark_armed(spec, False)
        queue = self.queue()
        if queue.get("pending"):
            self._archive(queue, "skipped", queue["pending"])
        return {"running": False, "stopped": True, "live_armed": False,
                "pid": stopped.get("pid")}

    def reconcile(self) -> dict:
        """Log a reconcile request. On-chain sync is not wired."""
        spec = self.task()
        entry = {
            "ts": self.now(),
            "routed": False,
            "task": bool(spec),
            "gap": ("on-chain reconcile is not wired; the request was logged "
                    "and no venue was contacted"),
        }
        path = self.dir / "reconcile.json"
        data = self._read_json(path) or {"history": []}
        history = data.get("history")
        if not isinstance(history, list):
            history = []
        history.append(entry)
        data["history"] = history[-_QUEUE_LIMIT:]
        data["last"] = entry
        self._write_json(path, data)
        return {"ok": True, "routed": False, "ts": entry["ts"], "gap": entry["gap"]}

    # --------------------------------------------------------------- confirm

    def _edge_view(self, latest: Optional[dict]) -> dict:
        spec = self.task() or decision_defaults()
        tob = (latest or {}).get("tob") or {}
        fill = (latest or {}).get("fillable_100") or {}

        def _f(value):
            if value is None or value == "":
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        sell_fill = _f(fill.get("sell_bps"))
        buy_fill = _f(fill.get("buy_bps"))
        sell_pre = sell_fill if sell_fill is not None else _f(tob.get("sell_edge_mean_bps"))
        buy_pre = buy_fill if buy_fill is not None else _f(tob.get("buy_edge_mean_bps"))
        premium = _f(tob.get("premium_close_bps"))
        midline = spec.get("midline_bps")
        deviation = None
        if premium is not None and isinstance(midline, (int, float)):
            deviation = premium - float(midline)
        return {
            "fill_sell_bps": sell_fill,
            "fill_buy_bps": buy_fill,
            "sell_pre_bps": sell_pre,
            "buy_pre_bps": buy_pre,
            "net_sell_bps": net_edge_bps(sell_pre),
            "net_buy_bps": net_edge_bps(buy_pre),
            "premium_close_bps": premium,
            "midline_bps": midline,
            "deviation_bps": deviation,
            "fee_bps": ENTROPY_FEE_BPS,
        }

    def _history_nets(self, direction: str) -> list:
        rel = recorder_csv_rel(self.root)
        rows = read_minutes(self.root / rel).get("rows") or []
        key = "sell_bps" if direction == "sell_entropy" else "buy_bps"
        tob_key = ("sell_edge_mean_bps" if direction == "sell_entropy"
                   else "buy_edge_mean_bps")
        nets = []
        for row in rows:
            raw = (row.get("fillable_100") or {}).get(key)
            if raw in (None, ""):
                raw = (row.get("tob") or {}).get(tob_key)
            try:
                pre = float(raw)
            except (TypeError, ValueError):
                continue
            nets.append(net_edge_bps(pre))
        return nets

    def _accounts(self, force: bool = False) -> dict:
        now = float(self.now())
        if (not force and self._account_cache is not None
                and now - self._accounts_at < 30):
            return self._account_cache
        try:
            snap = self.account_reader()
        except Exception:
            snap = {"creds": {"entropy": False, "lighter": False},
                    "entropy": {}, "lighter": {},
                    "note": "账户读取未完成"}
        self._account_cache = scrub(snap)
        self._accounts_at = now
        return self._account_cache

    def _entry(self) -> dict:
        data = self._read_json(self.entry_path)
        return data if isinstance(data, dict) else {}

    def _save_entry(self, entry: Optional[dict]) -> None:
        if not entry:
            try:
                self.entry_path.unlink()
            except FileNotFoundError:
                pass
            return
        self._write_json(self.entry_path, entry)

    def _daily(self) -> dict:
        day = trading_day(self._clock())
        data = self._read_json(self.daily_path) or {}
        if data.get("day") != day:
            return {"day": day, "notional_usd": 0.0, "count": 0}
        try:
            notional = float(data.get("notional_usd") or 0.0)
        except (TypeError, ValueError):
            notional = 0.0
        try:
            count = int(data.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        return {"day": day, "notional_usd": notional, "count": count}

    def _bump_daily(self, notional: float) -> None:
        day = self._daily()
        day["notional_usd"] = float(day["notional_usd"]) + float(notional)
        day["count"] = int(day["count"]) + 1
        self._write_json(self.daily_path, day)

    def _clear_revert(self) -> None:
        self._revert_since = None
        self._revert_watch = None

    def _drop_pending(self, queue: dict) -> None:
        if queue.get("pending"):
            self._save_queue({"pending": None, "history": queue["history"]})

    def _close_notional(self, qty: float, latest: Optional[dict],
                        fallback: float) -> float:
        tob = (latest or {}).get("tob") or {}
        try:
            bid = float(tob.get("entropy_bid"))
            ask = float(tob.get("entropy_ask"))
        except (TypeError, ValueError):
            return float(fallback)
        if bid <= 0 or ask <= 0:
            return float(fallback)
        return float(qty) * (bid + ask) / 2.0

    def _pos_sig(self, accounts: dict):
        def _r(value):
            try:
                if isinstance(value, bool):
                    return None
                return round(float(value), 8)
            except (TypeError, ValueError):
                return None
        ent = (accounts.get("entropy") or {}).get("position")
        lig = (accounts.get("lighter") or {}).get("position")
        return (_r(ent), _r(lig))

    def refresh_proposal(self, latest: Optional[dict]) -> Optional[dict]:
        """Open a confirm card, or a reduce-only close after an inside-band revert.

        Record mode never proposes. A symmetric Entropy/Lighter book is
        not an open: premium must sit inside the task band
        (midline − lower <= premium <= midline + upper, edges included)
        for ``persist_sec`` before a close card is raised. Outside that
        band the close timer resets. Flat books keep the open path.
        """
        spec = self.task()
        queue = self.queue()
        if not spec or spec.get("mode") != "live" or not spec.get("live_armed"):
            self._drop_pending(queue)
            self._clear_revert()
            return None
        if not spec.get("manual_confirm"):
            self._clear_revert()
            return None
        accounts = self._accounts()
        held = symmetric_position(
            (accounts.get("entropy") or {}).get("position"),
            (accounts.get("lighter") or {}).get("position"))
        if held is not None:
            return self._refresh_close(spec, queue, latest, accounts, held)
        self._clear_revert()
        return self._refresh_open(spec, queue, latest, accounts)

    def _refresh_close(self, spec: dict, queue: dict, latest: Optional[dict],
                       accounts: dict, held: dict) -> Optional[dict]:
        view = self._edge_view(latest)
        premium = view.get("premium_close_bps")
        persist = float(spec.get("persist_sec") or PERSIST_SEC)
        if not premium_inside_band(premium, spec):
            self._clear_revert()
            self._drop_pending(queue)
            return None
        now = float(self.now())
        if self._revert_since is None:
            self._revert_since = now
        elapsed = max(0.0, now - float(self._revert_since))
        ready = elapsed + 1e-9 >= persist
        self._revert_watch = {
            "inside": True,
            "elapsed_sec": round(elapsed, 3),
            "persist_sec": persist,
            "ready": ready,
            "rule": "inside_band",
        }
        if not ready:
            pending = queue.get("pending")
            if pending and pending.get("intent") == "close":
                self._drop_pending(queue)
            elif pending and pending.get("intent") != "close":
                self._drop_pending(queue)
            return None
        notional = self._close_notional(
            held["qty"], latest, float(spec["order_notional_usd"]))
        deviation = None
        if premium is not None and spec.get("midline_bps") is not None:
            deviation = round(float(premium) - float(spec["midline_bps"]), 4)
        pending = queue.get("pending")
        if (pending and pending.get("intent") == "close"
                and pending.get("direction") == held["direction"]):
            pending["pre_fee_edge_bps"] = (
                round(float(premium), 4) if premium is not None else None)
            pending["net_edge_bps"] = deviation
            pending["close_qty"] = held["qty"]
            pending["legs"] = leg_plan(
                held["direction"], notional, accounts, base_qty=held["qty"])
            pending["rth"] = self.rth_now()
            self._save_queue(queue)
            return pending
        proposal = {
            "proposal_id": uuid.uuid4().hex,
            "intent": "close",
            "reduce_only": True,
            "close_qty": held["qty"],
            "held": held["held"],
            "direction": held["direction"],
            "pre_fee_edge_bps": (
                round(float(premium), 4) if premium is not None else None),
            "net_edge_bps": deviation if deviation is not None else 0.0,
            "legs": leg_plan(
                held["direction"], notional, accounts, base_qty=held["qty"]),
            "tail_vs_median": None,
            "rth": self.rth_now(),
            "created_at": self.now(),
        }
        queue["pending"] = proposal
        self._save_queue(queue)
        return proposal

    def _refresh_open(self, spec: dict, queue: dict, latest: Optional[dict],
                      accounts: dict) -> Optional[dict]:
        view = self._edge_view(latest)
        qual = qualifying_direction(spec, view["sell_pre_bps"], view["buy_pre_bps"])
        if qual is None:
            self._sizing_note = None
            self._drop_pending(queue)
            return None
        sized = size_order_notional(
            mode=spec.get("sizing_mode") or "cash",
            hard_max_usd=float(spec["order_notional_usd"]),
            accounts=accounts,
            safety=float(spec.get("margin_safety") or MARGIN_SAFETY),
            min_usd=(MIN_ORDER_USD if spec.get("sizing_mode") == "margin"
                     else 0.0),
        )
        self._sizing_note = sized.get("reason")
        if sized.get("refused") or sized.get("notional_usd") is None:
            self._drop_pending(queue)
            return None
        notional = float(sized["notional_usd"])
        pending = queue.get("pending")
        if pending and pending.get("intent") not in (None, "open"):
            pending = None
        if pending and pending.get("direction") == qual["direction"]:
            pending["intent"] = "open"
            pending["reduce_only"] = False
            pending["net_edge_bps"] = qual["net_edge_bps"]
            pending["pre_fee_edge_bps"] = qual["pre_fee_edge_bps"]
            pending["tail_vs_median"] = tail_vs_median(
                self._history_nets(qual["direction"]), qual["net_edge_bps"])
            pending["rth"] = self.rth_now()
            self._save_queue(queue)
            return pending
        proposal = {
            "proposal_id": uuid.uuid4().hex,
            "intent": "open",
            "reduce_only": False,
            "direction": qual["direction"],
            "pre_fee_edge_bps": qual["pre_fee_edge_bps"],
            "net_edge_bps": qual["net_edge_bps"],
            "legs": leg_plan(qual["direction"], notional, accounts),
            "tail_vs_median": tail_vs_median(
                self._history_nets(qual["direction"]), qual["net_edge_bps"]),
            "rth": self.rth_now(),
            "created_at": self.now(),
            "sizing": sized.get("source"),
        }
        queue["pending"] = proposal
        self._save_queue(queue)
        return proposal

    def pending_payload(self, latest: Optional[dict]) -> Optional[dict]:
        spec = self.task()
        pending = self.refresh_proposal(latest)
        if not spec or not pending:
            return None
        rth = self.rth_now()
        payload = build_confirm_payload(
            spec, pending, rth=rth, tail=pending.get("tail_vs_median"))
        snap = session_snapshot(self._clock(), float(spec["midline_bps"]))
        block = self._block_reason()
        # Outside RTH the card stays clickable. The first click only
        # acknowledges the force card; the order waits for 「强制确认」.
        payload["confirm_enabled"] = block is None and bool(spec.get("manual_confirm"))
        payload["block_reason"] = block
        payload["rth_only"] = bool(spec.get("rth_only", True))
        payload["force_required"] = not rth
        payload["force_ack"] = bool(pending.get("force_ack"))
        payload["session"] = snap
        payload["force_card"] = snap if not rth else None
        gate = self._auto_gate(
            spec, pending, latest, rth=rth,
            halted=bool(self.risk().get("halted")))
        payload["round_trip_fee_bps"] = gate["round_trip_fee_bps"]
        payload["round_trip_net_bps"] = gate["round_trip_net_bps"]
        payload["auto_confirm"] = bool(spec.get("auto_confirm"))
        payload["auto_blocked"] = None
        if spec.get("auto_confirm"):
            if not rth:
                payload["auto_blocked"] = "非 RTH：自动确认不会强制发单"
            elif not payload["confirm_enabled"]:
                payload["auto_blocked"] = block
            elif gate["ok"]:
                if self._auto_fire(spec, pending):
                    return None
                payload["auto_blocked"] = "自动确认冷却中，仓位未变"
            else:
                payload["auto_blocked"] = gate["reason"]
        return payload

    def _leg_notional(self, pending: dict, spec: dict) -> float:
        legs = pending.get("legs") or []
        if legs and isinstance(legs[0], dict):
            try:
                return float(legs[0].get("notional_usd"))
            except (TypeError, ValueError):
                pass
        return float(spec["order_notional_usd"])

    def _available_needs(self, spec: dict, notional: float, accounts: dict):
        if spec.get("sizing_mode") != "margin":
            return notional, notional
        try:
            ent_lev = float((accounts.get("entropy") or {})["leverage"])
            lig_lev = float((accounts.get("lighter") or {})["leverage"])
            if ent_lev <= 0 or lig_lev <= 0:
                raise ValueError
            return notional / ent_lev, notional / lig_lev
        except (KeyError, TypeError, ValueError):
            return notional, notional

    def _auto_gate(self, spec: dict, pending: dict, latest: Optional[dict],
                   *, rth: bool, halted: bool) -> dict:
        accounts = self._accounts()
        ent = accounts.get("entropy") or {}
        lig = accounts.get("lighter") or {}
        fund = (latest or {}).get("funding") or {}
        notional = self._leg_notional(pending, spec)
        need_e, need_l = self._available_needs(spec, notional, accounts)
        daily = self._daily()
        entry = self._entry()
        measured = self.risk().get("actual_fee_bps")
        return evaluate_auto_gates(
            intent=pending.get("intent") or "open",
            pre_fee_edge_bps=pending.get("pre_fee_edge_bps"),
            entry_pre_fee_bps=entry.get("pre_fee_edge_bps"),
            measured_fee_bps=measured,
            notional_usd=notional,
            auto_confirm_max_usd=float(spec.get("auto_confirm_max_usd")
                                       or spec["order_notional_usd"]),
            fresh=books_fresh(latest, now=float(self.now())),
            halted=bool(halted or self.risk().get("halted")),
            rth=bool(rth),
            funding_entropy=fund.get("entropy"),
            funding_hedge=fund.get("hedge"),
            available_entropy=ent.get("available"),
            available_lighter=lig.get("available"),
            available_need_entropy=need_e,
            available_need_lighter=need_l,
            daily_notional=daily["notional_usd"],
            daily_count=daily["count"],
            daily_max_notional=spec.get("auto_daily_max_notional_usd"),
            daily_max_count=spec.get("auto_daily_max_count"),
        )

    def _auto_fire(self, spec: dict, pending: dict) -> bool:
        """Send one auto order. False when the same position already auto-fired."""
        now = float(self.now())
        if now < float(self._auto_block_until):
            return False
        accounts = self._accounts()
        sig = self._pos_sig(accounts)
        intent = pending.get("intent") or "open"
        last = self._last_auto or {}
        if last.get("sig") == sig and last.get("intent") == intent:
            return False
        self._auto_block_until = now + float(spec.get("persist_sec") or PERSIST_SEC)
        queue = self.queue()
        current = queue.get("pending") or {}
        if current.get("proposal_id") != pending.get("proposal_id"):
            return False
        result = self._route_admitted(spec, queue, current, outside=False, auto=True)
        if result.get("sent"):
            self._last_auto = {"sig": sig, "intent": intent}
            self._bump_daily(self._leg_notional(current, spec))
            self._account_cache = None
        return True

    def _archive(self, queue: dict, action: str, proposal: dict,
                 **extra) -> None:
        queue["history"] = list(queue.get("history") or [])
        entry = {
            "action": action,
            "ts": self.now(),
            "proposal_id": proposal.get("proposal_id"),
            "routed": False,
            "sent": False,
        }
        for key, value in extra.items():
            if key in ("routed", "sent", "confirm_id", "force_confirm_outside_rth",
                       "error", "status"):
                entry[key] = value
        queue["history"].append(entry)
        queue["pending"] = None
        self._save_queue(queue)

    def cancel_confirm(self) -> dict:
        queue = self.queue()
        pending = queue.get("pending")
        if not pending:
            raise ProbeError("no pending confirm", status_code=409)
        self._archive(queue, "skipped", pending)
        return {"ok": True, "skipped": True, "sent": False, "routed": False,
                "proposal_id": pending.get("proposal_id")}

    def admit_confirm(self, payload: dict) -> dict:
        """Admit a confirm id, then route one dual-leg order.

        Outside US RTH the first call only records that the force card was
        seen. The order is sent on a later call with ``force_confirm`` true,
        and that path logs ``force_confirm_outside_rth=true``. Inside RTH one
        confirm is enough. Nothing is sent without a confirm id.
        """
        if not isinstance(payload, dict):
            raise ProbeError("confirm body must be an object", status_code=400)
        field_errors = confirm_field_errors(payload)
        if field_errors:
            raise ProbeError(
                "confirm payload rejected: " + "; ".join(field_errors),
                status_code=400)
        spec = self._load_task()
        if spec.get("mode") != "live" or not spec.get("live_armed"):
            raise ProbeError("confirm refused: probe is not live-armed",
                             status_code=409)
        if not spec.get("manual_confirm"):
            raise ProbeError(
                DECISION_WARNING + "；confirm refused without manual confirm",
                status_code=409)
        if payload.get("confirm") is not True:
            raise ProbeError("confirm refused without an explicit confirm",
                             status_code=409)
        proc = self.ctl.snapshot()
        if proc.get("paused"):
            raise ProbeError("confirm refused while paused", status_code=409)
        block = self._block_reason()
        if block:
            self._log("confirm refused: " + block)
            raise ProbeError(block, status_code=409)
        rth = self.rth_now()
        if bool(payload.get("rth")) != rth:
            raise ProbeError("confirm refused: rth flag does not match the server clock",
                             status_code=409)
        queue = self.queue()
        pending = queue.get("pending")
        if not pending:
            raise ProbeError("confirm refused: no pending proposal", status_code=409)
        if payload.get("proposal_id") != pending.get("proposal_id"):
            raise ProbeError("confirm refused: proposal_id does not match",
                             status_code=409)
        try:
            if abs(float(payload["net_edge_bps"]) - float(pending["net_edge_bps"])) > 0.02:
                raise ProbeError("confirm refused: net_edge_bps does not match",
                                 status_code=409)
            if abs(float(payload["midline_bps"]) - float(spec["midline_bps"])) > 1e-6:
                raise ProbeError("confirm refused: midline_bps does not match",
                                 status_code=409)
            order_usd = float(spec["order_notional_usd"])
            pos_usd = float(spec["max_position_usd"])
        except (TypeError, ValueError):
            raise ProbeError("confirm refused: numeric fields are not numbers",
                             status_code=400)
        if order_usd - 10.0 > 1e-9 or pos_usd - 10.0 > 1e-9:
            raise ProbeError("confirm refused: probe caps are $10", status_code=409)
        if not rth:
            force = payload.get("force_confirm") is True
            if not force:
                pending["force_ack"] = True
                self._save_queue(queue)
                self._log(
                    "force-ack proposal_id="
                    + str(pending.get("proposal_id"))
                    + " no order")
                card = session_snapshot(self._clock(), float(spec["midline_bps"]))
                return {
                    "ok": False,
                    "queued": False,
                    "routed": False,
                    "sent": False,
                    "needs_force_confirm": True,
                    "proposal_id": pending.get("proposal_id"),
                    "force_card": card,
                    "gap": "非 RTH：需要第二次点击「强制确认」才会发单",
                }
            if not pending.get("force_ack"):
                raise ProbeError(
                    "强制确认需要先确认风险提示", status_code=409)
        return self._route_admitted(spec, queue, pending, outside=not rth)

    def _route_admitted(self, spec: dict, queue: dict, pending: dict,
                        *, outside: bool, auto: bool = False) -> dict:
        confirm_id = uuid.uuid4().hex
        if not confirm_id:
            raise ProbeError("refusing order without an admitted confirm id",
                             status_code=500)
        direction = pending.get("direction")
        reduce_only = bool(pending.get("reduce_only"))
        self._log(
            f"route confirm_id={confirm_id} direction={direction} "
            f"intent={pending.get('intent') or 'open'} "
            f"reduce_only={str(reduce_only).lower()} "
            f"auto_confirm={str(bool(auto)).lower()} "
            f"force_confirm_outside_rth={str(bool(outside)).lower()}")
        # Drop the pending card before the send so a second click cannot
        # admit the same proposal again.
        proposal = dict(pending)
        queue["pending"] = None
        self._save_queue(queue)
        req = {
            "confirm_id": confirm_id,
            "direction": direction,
            "order_notional_usd": float(spec["order_notional_usd"]),
            "max_position_usd": float(spec["max_position_usd"]),
            "root": str(self.root),
            "force_confirm_outside_rth": bool(outside),
            "reduce_only": reduce_only,
            "close_qty": pending.get("close_qty"),
            "intent": pending.get("intent") or "open",
            "auto_confirm": bool(auto),
        }
        try:
            result = self.executor(req)
        except Exception as exc:
            result = {
                "ok": False, "routed": False, "sent": False, "halted": False,
                "confirm_id": confirm_id,
                "error": scrub(f"{type(exc).__name__}: {exc}"),
                "buy_fill": 0.0, "sell_fill": 0.0, "net_base": 0.0,
                "entropy_fee_bps": None, "status": "error",
            }
        if not isinstance(result, dict):
            result = {"ok": False, "routed": False, "sent": False,
                      "error": "bad executor result", "confirm_id": confirm_id}
        if result.get("confirm_id") not in (None, confirm_id):
            result["error"] = "confirm id mismatch"
            result["sent"] = False
        result.setdefault("confirm_id", confirm_id)
        self._apply_result(result)
        routed = bool(result.get("routed"))
        sent = bool(result.get("sent"))
        self._archive(queue, "confirmed" if sent else "failed", proposal,
                      routed=routed, sent=sent, confirm_id=confirm_id,
                      force_confirm_outside_rth=bool(outside),
                      error=result.get("error"), status=result.get("status"))
        # _archive rewrote the queue from the pre-clear copy's history.
        # pending is set to None there. Good.
        risk = self.risk()
        if sent and not risk.get("halted"):
            if reduce_only:
                self._save_entry(None)
                self._clear_revert()
            else:
                self._save_entry({
                    "direction": direction,
                    "pre_fee_edge_bps": pending.get("pre_fee_edge_bps"),
                    "notional_usd": self._leg_notional(pending, spec),
                    "ts": self.now(),
                })
        self._log(
            f"result confirm_id={confirm_id} routed={str(routed).lower()} "
            f"sent={str(sent).lower()} ok={str(bool(result.get('ok'))).lower()} "
            f"halt={risk.get('halt_reason') or ''} "
            f"fee_bps={result.get('entropy_fee_bps')} "
            f"error={result.get('error') or ''}")
        gap = result.get("error") or (
            "两腿已提交" if sent else "确认已承认，但没有发出订单")
        return scrub({
            "ok": bool(result.get("ok")),
            "queued": False,
            "routed": routed,
            "sent": sent,
            "needs_force_confirm": False,
            "proposal_id": proposal.get("proposal_id"),
            "confirm_id": confirm_id,
            "force_confirm_outside_rth": bool(outside),
            "reduce_only": reduce_only,
            "auto_confirm": bool(auto),
            "intent": pending.get("intent") or "open",
            "halted": bool(risk.get("halted")),
            "fee_mismatch": bool(risk.get("fee_mismatch")),
            "entropy_fee_bps": result.get("entropy_fee_bps"),
            "assumed_fee_bps": ENTROPY_FEE_BPS,
            "status": result.get("status"),
            "gap": gap,
        })

    def _apply_result(self, result: dict) -> None:
        risk = self.risk()
        buy = float(result.get("buy_fill") or 0.0)
        sell = float(result.get("sell_fill") or 0.0)
        net = result.get("net_base")
        if net is None:
            net = buy - sell
        try:
            net = float(net)
        except (TypeError, ValueError):
            net = buy - sell
        if result.get("halted") or abs(net) > NET_TOL_BASE:
            risk["halted"] = True
            risk["halt_reason"] = result.get("halt_reason") or "single-leg fill"
            risk["net_base"] = net
        elif result.get("sent"):
            risk["net_base"] = net
        actual = result.get("entropy_fee_bps")
        filled = buy > 0 or sell > 0
        if filled and actual is not None:
            try:
                actual_f = float(actual)
            except (TypeError, ValueError):
                actual_f = None
            if actual_f is not None:
                risk["actual_fee_bps"] = actual_f
                if abs(actual_f - ENTROPY_FEE_BPS) > FEE_MISMATCH_TOL_BPS:
                    risk["fee_mismatch"] = True
        risk["last_order"] = scrub({
            "confirm_id": result.get("confirm_id"),
            "ok": bool(result.get("ok")),
            "routed": bool(result.get("routed")),
            "sent": bool(result.get("sent")),
            "status": result.get("status"),
            "error": result.get("error"),
            "buy_fill": buy,
            "sell_fill": sell,
            "net_base": net,
            "entropy_fee_bps": risk.get("actual_fee_bps"),
            "assumed_fee_bps": ENTROPY_FEE_BPS,
            "ts": self.now(),
        })
        self._save_risk(risk)

    # ------------------------------------------------------------------ status

    def last_reconcile(self) -> Optional[dict]:
        data = self._read_json(self.dir / "reconcile.json") or {}
        last = data.get("last")
        return last if isinstance(last, dict) else None

    def overlay(self, base: dict) -> dict:
        spec = self.task()
        proc_running = bool(base.get("running"))
        paused = bool(base.get("paused"))
        live_armed = bool(spec and spec.get("live_armed") and proc_running)
        if spec and spec.get("live_armed") and not proc_running:
            self._mark_armed(spec, False)
            live_armed = False
            spec = self.task()
        fresh = books_fresh(base.get("latest"), now=float(self.now()))
        mode = spec.get("mode") if spec else None
        label = status_label(running=proc_running, paused=paused, mode=mode,
                             live_armed=live_armed, fresh=fresh)
        risk = self.risk()
        if risk.get("halted") or abs(float(risk.get("net_base") or 0.0)) > NET_TOL_BASE:
            label = STATUS_HALT
        proposal = None
        if spec and live_armed and not paused:
            proposal = self.pending_payload(base.get("latest"))
        elif self.queue().get("pending") and not live_armed:
            self._save_queue({"pending": None, "history": self.queue()["history"]})
            self._clear_revert()
        out = dict(base)
        out["paused"] = paused
        out["status_label"] = label
        out["task"] = self._public_task(spec)
        out["live_armed"] = live_armed
        out["rth"] = self.rth_now()
        out["rth_window"] = RTH_WINDOW
        out["edge"] = self._edge_view(base.get("latest"))
        out["proposal"] = proposal
        out["accounts"] = self._accounts()
        out["reconcile"] = self.last_reconcile()
        out["confirm_queue"] = {
            "pending": bool(proposal),
            "recent": [
                {"action": item.get("action"), "ts": item.get("ts"),
                 "proposal_id": item.get("proposal_id"),
                 "confirm_id": item.get("confirm_id"),
                 "routed": bool(item.get("routed")),
                 "sent": bool(item.get("sent")),
                 "force_confirm_outside_rth": bool(
                     item.get("force_confirm_outside_rth"))}
                for item in self.queue().get("history") or []
            ][-5:],
        }
        out["halted"] = label == STATUS_HALT
        out["fee_check"] = {
            "assumed_bps": ENTROPY_FEE_BPS,
            "actual_bps": risk.get("actual_fee_bps"),
            "mismatch": bool(risk.get("fee_mismatch")),
        }
        out["last_order"] = risk.get("last_order")
        out["net_base"] = risk.get("net_base")
        task_mid = float(spec["midline_bps"]) if spec else -1.7
        out["session"] = session_snapshot(self._clock(), task_mid)
        out["revert_watch"] = self._revert_watch
        out["sizing_note"] = self._sizing_note
        out["auto_confirm"] = bool(spec and spec.get("auto_confirm"))
        out["round_trip_fee_bps"] = round_trip_fee_bps(risk.get("actual_fee_bps"))
        out["gaps"] = [
            "启动探针实盘仍只拉起 --record-only 记录进程。下单只发生在已承认的确认单上，并走 Engine.execute_confirmed。",
            "对称持仓（Entropy 空 / Lighter 多，或相反）时，溢价回到任务带宽内（含边界）并持续 persist_sec（3 秒）才提出只减仓平仓。不是碰到中枢才平。",
            "自动确认默认关闭。打开后，开仓和平仓都要过往返门槛（Entropy 开+平约 1.8 bps，不是单边 0.9），且非 RTH 不会自动走强制确认。",
            "保证金缩放只在两边都读到可用保证金和杠杆时启用，并受单笔硬顶约束。缺失则拒绝，不拿现金名义冒充。",
            "非美股 RTH 不禁用确认，但必须先看强制确认卡，再点「强制确认」才会发单。",
            "默认仍建议仅美国 RTH 启动。中枢不会按时段自动切换。",
            "对账只记一条请求，不会做链上持仓同步。",
            "RTH 为美东周一至周五 09:30–16:00，不含交易所假日。",
            "单腿成交会 HALT，净敞口不为 0 时拒绝新开仓，也拒绝平仓确认。",
        ]
        return scrub(out)


def _python() -> str:
    import sys
    return sys.executable
