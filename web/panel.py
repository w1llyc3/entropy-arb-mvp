"""FastAPI panel for the SNDK Entropy ↔ Lighter probe.

Binds nowhere by itself. ``python3 -m web`` listens on 127.0.0.1 only.
Secrets stay in the server ``.env`` and are scrubbed from every response.
Starting the panel does not arm live trading. Confirm admits an id and then
calls ``Engine.execute_confirmed``. Outside US RTH that send waits for a
second 「强制确认」. Arming live outside that window waits for 「强制启动」.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from entropy_arb.config import HEDGE_VENUES

from web.recorder_ctl import RecorderControl, RecorderError
from web.report import assemble_status, run_analyze
from web.session import ProbeError, ProbeSession

_INDEX = Path(__file__).with_name("index.html")
_SYMBOL = re.compile(r"^[A-Z0-9]{1,16}$")


class StartIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str = "SNDK"
    hedge: str = "lighter"
    force_start_outside_rth: bool = False


def normalize_symbol(value: str) -> str:
    text = (value or "").strip().upper()
    if not _SYMBOL.fullmatch(text):
        raise HTTPException(
            status_code=400,
            detail="symbol must be 1–16 letters or digits")
    return text


def normalize_hedge(value: str) -> str:
    key = (value or "").strip().lower()
    for venue in HEDGE_VENUES:
        if venue == key:
            return venue
    raise HTTPException(
        status_code=400,
        detail=f"hedge must be one of: {', '.join(HEDGE_VENUES)}")


class TaskIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    midline_bps: float = -1.7
    upper_bps: float = 1.0
    lower_bps: float = 1.0
    order_notional_usd: float = 10.0
    max_position_usd: float = 10.0
    mode: str = "record"
    manual_confirm: bool = True
    rth_only: bool = True


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="allow")


def _probe(exc: ProbeError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc))


def _recorder(exc: RecorderError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=str(exc))


def create_app(root: Optional[Path] = None, command_builder=None,
               now=None, account_reader=None, executor=None) -> FastAPI:
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    ctl = RecorderControl(root, command_builder=command_builder)
    session = ProbeSession(root, ctl, now=now, account_reader=account_reader,
                           executor=executor)
    app = FastAPI(
        title="SNDK Entropy Lighter probe",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.root = root
    app.state.ctl = ctl
    app.state.session = session

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            _INDEX.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    def _status() -> dict:
        proc = ctl.snapshot()
        base = assemble_status(root, proc)
        base["paused"] = bool(proc.get("paused"))
        return session.overlay(base)

    @app.get("/api/probe")
    def probe_defaults() -> dict:
        return session.defaults()

    @app.get("/api/status")
    def status() -> dict:
        return _status()

    @app.post("/api/task")
    def save_task(body: TaskIn) -> dict:
        try:
            return session.save_task(body.model_dump())
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/task/clear")
    def clear_task() -> dict:
        try:
            return session.clear_task()
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/start")
    def start_record_only(body: StartIn) -> dict:
        """Record-only start. Extra fields such as a live flag are ignored."""
        symbol = normalize_symbol(body.symbol)
        hedge = normalize_hedge(body.hedge)
        try:
            info = ctl.start(symbol, hedge)
        except RecorderError as exc:
            _recorder(exc)
        spec = session.task()
        if spec and spec.get("mode") != "live":
            session._mark_armed(spec, False)
        return info

    @app.post("/api/session/start")
    def session_start(body: Optional[StartIn] = Body(default=None)) -> dict:
        """Arm or record. Outside RTH the first call warns; it does not arm.

        A bare POST is the first click (``force_start_outside_rth`` false).
        The record-only ``/api/start`` route ignores that flag.
        """
        force = bool(body.force_start_outside_rth) if body is not None else False
        try:
            return session.start(force_start_outside_rth=force)
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/pause")
    def pause() -> dict:
        try:
            return session.pause()
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/stop")
    def stop() -> dict:
        try:
            return session.stop()
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/reconcile")
    def reconcile() -> dict:
        try:
            return session.reconcile()
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/confirm")
    def confirm(body: ConfirmIn) -> dict:
        try:
            return session.admit_confirm(body.model_dump())
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/confirm/cancel")
    def confirm_cancel() -> dict:
        try:
            return session.cancel_confirm()
        except ProbeError as exc:
            _probe(exc)

    @app.post("/api/analyze")
    def analyze() -> dict:
        return run_analyze(root)

    return app


app = create_app()
