"""Dashboard web server. Runs the engine loop in the background."""

import asyncio
import csv
import io
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .engine import Engine

STATIC = Path(__file__).parent / "static"
security = HTTPBasic(auto_error=False)


def require_password(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
    """If DASHBOARD_PASSWORD is set, every request needs it (any username)."""
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password:
        return
    if creds is None or not secrets.compare_digest(creds.password.encode(), password.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})


def create_app(engine: Engine, run_loop: bool = True) -> FastAPI:
    async def loop():
        while True:
            await asyncio.to_thread(engine.tick)
            await asyncio.sleep(engine.s.bot.poll_seconds)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(loop()) if run_loop else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Survival Bot", lifespan=lifespan, dependencies=[Depends(require_password)])

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def get_status():
        return engine.summary()

    @app.get("/api/equity")
    def get_equity(days: int = 30):
        since = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        return engine.store.equity_history(since)

    @app.get("/api/trades")
    def get_trades(limit: int = 100):
        return engine.store.trades(limit)

    @app.get("/api/events")
    def get_events(limit: int = 100):
        return engine.store.events(limit)

    @app.post("/api/kill")
    async def kill():
        await asyncio.to_thread(engine.kill)
        return engine.summary()

    @app.post("/api/resume")
    async def resume():
        await asyncio.to_thread(engine.resume)
        return engine.summary()

    @app.get("/api/trades.csv")
    def trades_csv():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date_utc", "side", "asset", "qty", "price_usdt", "value_usdt", "fee_usdt", "gas_usdt",
                    "realised_pnl_usdt", "usdt_aud_rate", "value_aud", "realised_pnl_aud", "reason"])
        for t in reversed(engine.store.trades(100000)):
            rate = t["usd_aud"]
            w.writerow([
                datetime.fromtimestamp(t["ts"], timezone.utc).isoformat(), t["side"], engine.s.bot.asset,
                f"{t['qty']:.8f}", f"{t['price']:.2f}", f"{t['notional']:.2f}", f"{t['fee']:.4f}", f"{t['gas']:.4f}",
                "" if t["pnl"] is None else f"{t['pnl']:.4f}", rate or "",
                f"{t['notional'] * rate:.2f}" if rate else "",
                f"{t['pnl'] * rate:.4f}" if rate and t["pnl"] is not None else "", t["reason"],
            ])
        buf.seek(0)
        return StreamingResponse(buf, media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=trades.csv"})

    return app
