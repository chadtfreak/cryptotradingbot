"""Dashboard web server. Runs the engine loop in the background."""

import asyncio
import csv
import io
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, status
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


def create_app(engines: "dict[str, Engine] | Engine", run_loop: bool = True) -> FastAPI:
    if isinstance(engines, Engine):
        engines = {engines.brain.name: engines}

    async def loop(engine: Engine):
        while True:
            await asyncio.to_thread(engine.tick)
            await asyncio.sleep(engine.s.bot.poll_seconds)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(loop(e)) for e in engines.values()] if run_loop else []
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Survival Bot", lifespan=lifespan, dependencies=[Depends(require_password)])

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def get(bot: str) -> Engine:
        if bot not in engines:
            raise HTTPException(404, f"No bot called {bot}")
        return engines[bot]

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/bots")
    def bots():
        return [e.summary() for e in engines.values()]

    @app.get("/api/{bot}/status")
    def get_status(bot: str):
        return get(bot).summary()

    @app.get("/api/{bot}/equity")
    def get_equity(bot: str, days: int = 30):
        since = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        return get(bot).store.equity_history(since)

    @app.get("/api/{bot}/trades")
    def get_trades(bot: str, limit: int = 100):
        return get(bot).store.trades(limit)

    @app.get("/api/{bot}/events")
    def get_events(bot: str, limit: int = 100):
        return get(bot).store.events(limit)

    @app.post("/api/{bot}/kill")
    async def kill(bot: str):
        engine = get(bot)
        await asyncio.to_thread(engine.kill)
        return engine.summary()

    @app.post("/api/{bot}/resume")
    async def resume(bot: str):
        engine = get(bot)
        await asyncio.to_thread(engine.resume)
        return engine.summary()

    @app.post("/api/claude/key")
    async def set_key(body: dict = Body(...)):
        engine = get("claude")
        key = (body.get("key") or "").strip()
        if not key.startswith("sk-ant-"):
            raise HTTPException(400, "That doesn't look like an Anthropic API key. It should start with sk-ant-.")
        ok, message = await asyncio.to_thread(check_key, key)
        if not ok:
            raise HTTPException(400, message)
        engine.store.set("anthropic_api_key", key)
        engine.store.set("claude_waiting_logged", False)
        engine.store.log("API key saved. I can think now.")
        return engine.summary()

    @app.delete("/api/claude/key")
    def delete_key():
        engine = get("claude")
        engine.store.set("anthropic_api_key", None)
        engine.store.log("API key removed. I'll stop thinking until a new one is added.", level="warning")
        return engine.summary()

    @app.get("/api/{bot}/trades.csv")
    def trades_csv(bot: str):
        engine = get(bot)
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
                                 headers={"Content-Disposition": f"attachment; filename={bot}-trades.csv"})

    return app


def check_key(key: str) -> tuple[bool, str]:
    """Checks the key works by listing models, which costs nothing."""
    try:
        import anthropic
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=20.0).models.list(limit=1)
        return True, "ok"
    except Exception as exc:
        name = type(exc).__name__
        if name == "AuthenticationError":
            return False, "Anthropic didn't accept that key. Check you copied all of it."
        return False, f"Couldn't check the key with Anthropic ({name}). Try again in a minute."
