"""Separate loopback observation workbench; never starts a trading/collector service.

Run: python -m web.workbench --port 8770
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from entropy_arb.ownership import OwnerLock
from entropy_arb.workbench import SessionConflict, SessionStore
from web.research_basis import build_research_basis

ROOT = Path(__file__).resolve().parents[1]


def create_app(*, state_dir=None, data_root=None):
    state_dir = Path(state_dir or ROOT / ".web" / "session-workbench").resolve()
    data_root = Path(data_root or ROOT).resolve()
    if state_dir == data_root / ".web" or state_dir == ROOT / ".web" / "manual-live-test":
        raise ValueError("workbench requires its own state directory")
    owner = OwnerLock(dict(workbench=str(state_dir)))

    @asynccontextmanager
    async def lifespan(app):
        if not owner.acquire():
            raise RuntimeError("workbench state already owned by another process")
        store = None
        timer = None
        try:
            store = SessionStore(state_dir / "sessions.sqlite")
            app.state.store = store
            store.recover()
            async def expire_sessions():
                while True:
                    store.expire()
                    await asyncio.sleep(1)
            timer = asyncio.create_task(expire_sessions())
            yield
        finally:
            if timer:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            if store:
                store.close()
            owner.close()

    app = FastAPI(title="SNDK session workbench · observation milestone", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_writes(request: Request, call_next):
        host = request.headers.get("host", "")
        hostname = host.split(":")[0]
        if hostname not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "loopback host required"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and origin not in ("http://" + host, "https://" + host):
                return JSONResponse({"detail": "same-origin commands required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(Path(__file__).with_name("workbench.html").read_text(encoding="utf-8"))

    @app.get("/assets/workbench.js")
    def script():
        return FileResponse(Path(__file__).with_name("workbench.js"), media_type="text/javascript")

    @app.get("/assets/chart-time.js")
    def chart_time():
        return FileResponse(Path(__file__).with_name("chart_time.js"), media_type="text/javascript")

    @app.get("/api/status")
    def status():
        return app.state.store.snapshot()

    @app.get("/api/basis")
    async def basis():
        return await asyncio.to_thread(build_research_basis, data_root, symbol="SNDK", hours=24)

    @app.post("/api/sessions")
    def create(body: dict = Body(...)):
        if set(body) - {"key", "rules"}:
            raise HTTPException(400, "unknown command fields")
        return command("create", body)

    def command(action, body, sid=None):
        try:
            app.state.store.expire()
            return app.state.store.command(action, body.get("key"), sid=sid,
                                           version=body.get("version"), rules=body.get("rules"))
        except SessionConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid rules or command") from None
        except KeyError:
            raise HTTPException(404, "unknown session") from None

    @app.post("/api/sessions/{sid}/{action}")
    def transition(sid: str, action: str, body: dict = Body(...)):
        if action not in ("start", "pause", "resume", "end"):
            raise HTTPException(404, "unknown action")
        if set(body) - {"key", "version"}:
            raise HTTPException(400, "unknown command fields")
        return command(action, body, sid)

    @app.get("/api/sessions/{sid}/report")
    def report(sid: str):
        try:
            return app.state.store.report(sid)
        except KeyError:
            raise HTTPException(404, "unknown session") from None

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".web" / "session-workbench")
    parser.add_argument("--data-root", type=Path, default=ROOT,
                        help="existing recorder repository; historical CSV read access only")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    if args.port == 8766:
        parser.error("8766 is reserved for the existing collector dashboard")
    import uvicorn
    uvicorn.run(create_app(state_dir=args.state_dir, data_root=args.data_root),
                host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
