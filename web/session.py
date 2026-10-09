"""Single SNDK probe task: record, arm, pause, reconcile, confirm queue.

Arming 探针实盘 never builds a live ``main.py`` command. The subprocess, when
one is started, is record-only. A confirm admits an intent to a local queue
and does not call a venue.
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

from web.accounts import read_accounts, scrub
from web.probe import (
    ACCRUAL_BPS,
    ACCRUAL_LABEL,
    DECISION_WARNING,
    ENTROPY_FEE_BPS,
    RTH_WINDOW,
    SYMBOL,
    books_fresh,
    build_confirm_payload,
    confirm_field_errors,
    decision_defaults,
    decision_warnings,
    in_us_rth,
    leg_plan,
    net_edge_bps,
    normalize_task,
    probe_config_dict,
    qualifying_direction,
    status_label,
    tail_vs_median,
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
                 account_reader: Optional[Callable] = None) -> None:
        self.root = Path(root)
        self.ctl = ctl
        self.now = now or time.time
        self.account_reader = account_reader or (
            lambda: read_accounts(self.root))
        self.dir = self.root / ".web"
        self.task_path = self.dir / "task.json"
        self.queue_path = self.dir / "confirm_queue.json"
        self.config_path = self.dir / "probe.yaml"
        self._account_cache = None
        self._accounts_at = 0.0

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

    def refresh_proposal(self, latest: Optional[dict]) -> Optional[dict]:
        """Open a confirm card when the live probe sees a qualifying net edge.

        Record mode never proposes. Outside RTH the card can still be shown
        with confirm disabled; ``admit_confirm`` refuses it.
        """
        spec = self.task()
        queue = self.queue()
        if not spec or spec.get("mode") != "live" or not spec.get("live_armed"):
            if queue.get("pending"):
                self._save_queue({"pending": None, "history": queue["history"]})
            return None
        if not spec.get("manual_confirm"):
            return None
        view = self._edge_view(latest)
        qual = qualifying_direction(spec, view["sell_pre_bps"], view["buy_pre_bps"])
        if qual is None:
            if queue.get("pending"):
                self._save_queue({"pending": None, "history": queue["history"]})
            return None
        pending = queue.get("pending")
        if pending and pending.get("direction") == qual["direction"]:
            pending["net_edge_bps"] = qual["net_edge_bps"]
            pending["pre_fee_edge_bps"] = qual["pre_fee_edge_bps"]
            pending["tail_vs_median"] = tail_vs_median(
                self._history_nets(qual["direction"]), qual["net_edge_bps"])
            pending["rth"] = self.rth_now()
            self._save_queue(queue)
            return pending
        proposal = {
            "proposal_id": uuid.uuid4().hex,
            "direction": qual["direction"],
            "pre_fee_edge_bps": qual["pre_fee_edge_bps"],
            "net_edge_bps": qual["net_edge_bps"],
            "legs": leg_plan(qual["direction"], spec["order_notional_usd"],
                             self._accounts()),
            "tail_vs_median": tail_vs_median(
                self._history_nets(qual["direction"]), qual["net_edge_bps"]),
            "rth": self.rth_now(),
            "created_at": self.now(),
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
        # Live confirm is refused outside US RTH even if the operator turned
        # the RTH-only arming toggle off. The toggle only gates 启动.
        payload["confirm_enabled"] = bool(rth) and bool(spec.get("manual_confirm"))
        payload["rth_only"] = bool(spec.get("rth_only", True))
        return payload

    def _archive(self, queue: dict, action: str, proposal: dict) -> None:
        queue["history"] = list(queue.get("history") or [])
        queue["history"].append({
            "action": action,
            "ts": self.now(),
            "proposal_id": proposal.get("proposal_id"),
            "routed": False,
            "sent": False,
        })
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
        """Queue a confirm only when live-armed, explicitly confirmed, and in RTH.

        Returns a body that always says the venue was not called.
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
        proc = self.ctl.snapshot()
        if proc.get("paused"):
            raise ProbeError("confirm refused while paused", status_code=409)
        rth = self.rth_now()
        if bool(payload.get("rth")) != rth:
            raise ProbeError("confirm refused: rth flag does not match the server clock",
                             status_code=409)
        if not rth:
            raise ProbeError(
                "confirm refused outside US RTH (America/New_York 09:30–16:00)",
                status_code=403)
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
        except (TypeError, ValueError):
            raise ProbeError("confirm refused: numeric fields are not numbers",
                             status_code=400)
        self._archive(queue, "confirmed", pending)
        return {
            "ok": True,
            "queued": True,
            "routed": False,
            "sent": False,
            "proposal_id": pending.get("proposal_id"),
            "gap": ("order routing is not wired; the intent was queued and "
                    "no venue order was sent"),
        }

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
        proposal = None
        if spec and live_armed and not paused:
            proposal = self.pending_payload(base.get("latest"))
        elif self.queue().get("pending") and not live_armed:
            self._save_queue({"pending": None, "history": self.queue()["history"]})
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
                 "routed": False, "sent": False}
                for item in self.queue().get("history") or []
            ][-5:],
        }
        out["gaps"] = [
            "探针实盘只武装确认队列；子进程始终是 --record-only，不会自动下单。",
            "确认入队后 routed=false：交易所订单路由未接入。",
            "对账只记一条请求，不会做链上持仓同步。",
            "RTH 为美东周一至周五 09:30–16:00，不含交易所假日。",
        ]
        return scrub(out)


def _python() -> str:
    import sys
    return sys.executable
