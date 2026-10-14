"""Thin API over one lifespan-owned service. GET only reads projections/history."""
from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import time

from fastapi import FastAPI,Body,HTTPException
from fastapi.responses import HTMLResponse,JSONResponse

from entropy_arb.service import TradingService,ServiceError
from entropy_arb.risk import RiskRejected
from entropy_arb.storage import IdempotencyConflict,RecoveryRequired,EvidenceConflict
from web.basis import build_basis
from web.report import assemble_status,run_analyze
from web.probe import decision_defaults


def create_app(root=None, *, public_accounts=None, service=None):
    root=Path(root or Path(__file__).resolve().parents[1]).resolve()
    service=service or TradingService(root,public_accounts=public_accounts)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try: yield
        finally: await service.close()

    app=FastAPI(title="Entropy persistent local service",lifespan=lifespan,
                docs_url=None,redoc_url=None,openapi_url=None)
    app.state.service=service
    app.state.root=root

    async def command(name, body=None):
        try: return await service.call(name,body)
        except (ServiceError,RiskRejected,IdempotencyConflict,RecoveryRequired,EvidenceConflict) as exc:
            raise HTTPException(409,str(exc)) from None
        except ValueError as exc: raise HTTPException(400,str(exc)) from None
        except Exception: raise HTTPException(503,"service command failed; recovery remains closed") from None

    @app.get("/",response_class=HTMLResponse)
    async def index():
        return HTMLResponse(Path(__file__).with_name("index.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control":"no-store"})

    @app.get("/api/status")
    async def status():
        snapshot=service.snapshot()
        # CSV is historical display only: it cannot arm, confirm or reconcile.
        history=await asyncio.to_thread(assemble_status,root,
            dict(running=snapshot.get("running",False),warnings=[],pid=snapshot.get("pid")))
        return JSONResponse(dict(history,**snapshot),headers={"Cache-Control":"no-store"})

    @app.get("/api/probe")
    async def defaults(): return decision_defaults()

    @app.get("/health/live")
    async def live():
        s=service.snapshot()
        healthy=bool(s) and time.time()-s.get("heartbeat",0)<3
        return JSONResponse(dict(alive=healthy,state=s.get("service_state")),status_code=200 if healthy else 503)

    @app.get("/health/ready")
    async def ready():
        s=service.snapshot()
        return JSONResponse(dict(ready=s.get("trading_ready",False),mode=s.get("mode"),
                                 reason=s.get("recovery_reason")),status_code=200 if s.get("trading_ready") else 503)

    @app.get("/api/operations/{operation_id}")
    async def operation(operation_id):
        try: return service.ledger.operation(operation_id)
        except KeyError: raise HTTPException(404,"unknown operation") from None

    @app.post("/api/task")
    async def task(body:dict=Body(...)): return await command("task",body)
    @app.post("/api/task/clear")
    async def clear(): return await command("clear")
    @app.post("/api/start")
    async def start(body:dict=Body(default={})): return await command("start",body)
    @app.post("/api/session/start")
    async def session_start(body:dict=Body(default={})): return await command("start",body)
    @app.post("/api/pause")
    async def pause(): return await command("pause")
    @app.post("/api/stop")
    async def stop(): return await command("stop")
    @app.post("/api/reconcile")
    async def reconcile(): return await command("reconcile")
    @app.post("/api/confirm")
    async def confirm(body:dict=Body(...)): return await command("confirm",body)
    @app.post("/api/confirm/cancel")
    async def cancel(): return await command("cancel")
    @app.post("/api/flatten")
    async def flatten(body:dict=Body(...)): return await command("flatten",body)
    @app.post("/api/analyze")
    async def analyze(): return await asyncio.to_thread(run_analyze,root)
    @app.get("/api/basis")
    async def basis(hours:float=0):
        if not 0<=hours<=24*366: hours=0
        return JSONResponse(await asyncio.to_thread(build_basis,root,hours=hours),headers={"Cache-Control":"no-store"})
    return app
