"""FastAPI panel: start/stop record-only, status, one-click analyze.

Binds nowhere by itself. ``python3 -m web`` listens on 127.0.0.1 only.
There is no route that trades, reads ``.env``, or accepts a CSV path.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from entropy_arb.config import HEDGE_VENUES

from .recorder_ctl import RecorderControl, RecorderError
from .report import assemble_status, run_analyze

_SYMBOL = re.compile(r"^[A-Z0-9]{1,16}$")
_INDEX = Path(__file__).with_name("index.html")


class StartIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str = "SNDK"
    hedge: str = "lighter"


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


def create_app(root: Optional[Path] = None, command_builder=None) -> FastAPI:
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    ctl = RecorderControl(root, command_builder=command_builder)
    app = FastAPI(
        title="entropy-arb record-only panel",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.root = root
    app.state.ctl = ctl

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(
            _INDEX.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/status")
    def status() -> dict:
        return assemble_status(root, ctl.snapshot())

    @app.post("/api/start")
    def start(body: StartIn) -> dict:
        symbol = normalize_symbol(body.symbol)
        hedge = normalize_hedge(body.hedge)
        try:
            return ctl.start(symbol, hedge)
        except RecorderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc))

    @app.post("/api/stop")
    def stop() -> dict:
        return ctl.stop()

    @app.post("/api/analyze")
    def analyze() -> dict:
        return run_analyze(root)

    return app


app = create_app()
