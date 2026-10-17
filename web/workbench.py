"""Separate loopback observation workbench; never starts a trading/collector service.

Run: python -m web.workbench --port 8770
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import aiohttp

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from entropy_arb.ownership import OwnerLock
from entropy_arb.workbench import SessionConflict, SessionStore
from entropy_arb.inventory_view import InventoryView, public_identity
from entropy_arb.watchlist import asset_for
from entropy_arb.paper_session import PaperSession
from entropy_arb.session_books import SessionBooks
from entropy_arb.continuity import ClockContinuity
from web.research_basis import build_research_basis, catalog

ROOT = Path(__file__).resolve().parents[1]


def create_app(*, state_dir=None, data_root=None, public_accounts=None, paper_feed=None):
    state_dir = Path(state_dir or ROOT / ".web" / "session-workbench").resolve()
    data_root = Path(data_root or ROOT).resolve()
    if state_dir == data_root / ".web" or state_dir == ROOT / ".web" / "manual-live-test":
        raise ValueError("workbench requires its own state directory")
    owner = OwnerLock(dict(workbench=str(state_dir)))
    inventory = InventoryView(public_accounts)
    feed=paper_feed or SessionBooks()

    @asynccontextmanager
    async def lifespan(app):
        if not owner.acquire():
            raise RuntimeError("workbench state already owned by another process")
        store = None
        timer = None
        account_timer = None
        http = None
        try:
            store = SessionStore(state_dir / "sessions.sqlite")
            app.state.store = store
            app.state.paper=PaperSession(store)
            store.recover()
            http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=7), trust_env=True)
            async def expire_sessions():
                continuity=ClockContinuity()
                while True:
                    try:
                        interrupted=continuity.observe()  # Idle time is not a scheduler stall.
                        store.expire()
                        current=store.snapshot()["active"]
                        if current:
                            sid=current["id"]
                            store.capture_real_evidence(sid,inventory.snapshot(current["rules"]["symbol"]))
                            if interrupted and current["state"] in ("OBSERVING","PAUSED","DRAINING"):
                                for book in feed.books.values(): book.clear()
                                with store.transaction():
                                    store.db.execute("UPDATE sessions SET state='RECOVERY_REQUIRED',version=version+1 WHERE id=?",(sid,))
                                    store.event(sid,"CLOCK_INTERRUPTION_REQUIRES_REVIEW")
                                current=store._session(sid)
                            symbol=current["rules"]["symbol"] if current["mode"]=="PAPER_SIMULATION" and current["state"] in ("OBSERVING","PAUSED","DRAINING") else None
                            await feed.select(symbol,http)
                            if symbol:
                                app.state.paper.tick(sid,feed.books)
                        else:
                            await feed.select(None,http)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        current=store.snapshot()["active"]
                        if current:
                            with store.transaction():
                                store.db.execute("UPDATE sessions SET state='RECOVERY_REQUIRED',version=version+1 WHERE id=?",(current["id"],))
                                store.event(current["id"],"PAPER_WORKER_INTERRUPTED_NO_RESEND")
                    await asyncio.sleep(.5)
            timer = asyncio.create_task(expire_sessions())
            if any(inventory.accounts.values()):
                async def refresh_inventory():
                    while True:
                        await inventory.refresh(http)
                        await asyncio.sleep(15)
                account_timer = asyncio.create_task(refresh_inventory())
            yield
        finally:
            if account_timer:
                account_timer.cancel()
                await asyncio.gather(account_timer, return_exceptions=True)
            if timer:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            await feed.close()
            if http:
                await http.close()
            if store:
                store.close()
            owner.close()

    app = FastAPI(title="Entropy session workbench · observation milestone", lifespan=lifespan,
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
        with app.state.store.lock:
            result=app.state.store.snapshot()
            if result["active"]:
                result["mode"]=result["active"]["mode"]
                result["paper"]=app.state.paper.projection(result["active"]["id"])
                result["paper"]["feed_error"]=getattr(feed,"error",None)
                result["real_account_reconciliation"]=app.state.store.real_evidence(result["active"]["id"])
            return result

    @app.get("/api/basis")
    async def basis(symbol: str = "SNDK"):
        try:
            return await asyncio.to_thread(build_research_basis, data_root, symbol=symbol, hours=24)
        except ValueError:
            raise HTTPException(400, "unknown research asset") from None

    @app.get("/api/assets")
    async def assets():
        return await asyncio.to_thread(catalog, data_root)

    @app.get("/api/inventory")
    def positions(symbol: str = "SNDK"):
        try:
            asset_for(symbol)
        except ValueError:
            raise HTTPException(400, "unknown research asset") from None
        return inventory.snapshot(symbol)

    @app.post("/api/sessions")
    def create(body: dict = Body(...)):
        if set(body) - {"key", "rules"}:
            raise HTTPException(400, "unknown command fields")
        return command("create", body)

    def command(action, body, sid=None):
        try:
            app.state.store.expire()
            if sid:
                current=app.state.store._session(sid)
                app.state.store.capture_real_evidence(sid,inventory.snapshot(current["rules"]["symbol"]))
                if action=="start" and current["state"]=="DRAFT" and current["mode"]=="PAPER_SIMULATION":
                    row=next(r for r in catalog(data_root)["assets"] if r["symbol"]==current["rules"]["symbol"])
                    if row["state"] in ("ENTROPY_ONLY","UNAVAILABLE"):
                        raise SessionConflict("paired market required for depth simulation; use observation mode")
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
        if action not in ("start", "pause", "resume", "end", "reconcile-paper"):
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
    parser.add_argument("--public-env-file", type=Path,
                        help="explicit file: select only public account identifiers; no keys or signing")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    if args.port == 8766:
        parser.error("8766 is reserved for the existing collector dashboard")
    try:
        accounts = public_identity(args.public_env_file) if args.public_env_file else None
    except Exception:
        parser.error("public account configuration unavailable or invalid; values are not displayed")
    import uvicorn
    uvicorn.run(create_app(state_dir=args.state_dir, data_root=args.data_root, public_accounts=accounts),
                host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
