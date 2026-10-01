"""The bot itself: wakes up, pays its bills, checks its health, manages risk and trades."""

import threading
import time
from datetime import datetime, timezone

from .broker import PaperBroker
from .config import Settings
from .store import Store
from .strategy import on_candle_close, position_size, stop_hit

RUNNING, PAUSED, STOPPED, DEAD = "running", "paused", "stopped", "dead"
COST_INTERVAL = 3600  # charge running costs hourly
EQUITY_BUCKET = 300  # keep one equity point per 5 minutes


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Engine:
    def __init__(self, settings: Settings, market, store: Store, broker=None, clock=time.time):
        self.s = settings
        self.market = market
        self.store = store
        self.broker = broker or PaperBroker(settings.costs)
        self.clock = clock
        self.lock = threading.Lock()
        self.last_price: float | None = None
        self.last_error: str | None = None
        self._init_account()

    def _init_account(self) -> None:
        if self.store.get("cash") is not None:
            return
        now = int(self.clock())
        bal = self.s.bot.starting_balance
        for key, value in {
            "cash": bal, "qty": 0.0, "cost_basis": 0.0, "stop": None, "status": RUNNING,
            "started_at": now, "last_cost_ts": now, "last_candle_ts": None,
            "day": utc_day(now), "day_start_equity": bal,
        }.items():
            self.store.set(key, value)
        self.store.add_ledger(now, "deposit", bal, "Starting balance (paper)")
        self.store.log(f"Born with {bal:.2f} USDT in paper mode. Running costs are "
                       f"{self.s.survival.monthly_running_cost_usd:.2f} USD a month, paid from my own balance.", ts=now)

    # Account helpers
    @property
    def cash(self) -> float:
        return self.store.get("cash")

    @property
    def qty(self) -> float:
        return self.store.get("qty")

    @property
    def status(self) -> str:
        return self.store.get("status")

    def equity(self, price: float) -> float:
        return self.cash + self.qty * price

    # Main loop step
    def tick(self) -> None:
        with self.lock:
            try:
                self._tick()
                self.last_error = None
            except Exception as exc:  # network blips etc. Log and try again next poll.
                self.last_error = str(exc)
                self.store.log(f"Tick failed: {exc}", level="error")

    def _tick(self) -> None:
        now = int(self.clock())
        price = self.market.price()
        self.last_price = price
        if self.status == DEAD:
            return

        self._pay_running_costs(now)
        equity = self.equity(price)
        self._roll_day(now, equity)
        self.store.add_equity(now - now % EQUITY_BUCKET, equity, self.cash, self.qty, price)

        if equity < self.s.survival.floor_usd:
            self._close(price, now, f"Equity {equity:.2f} fell below the survival floor of "
                                    f"{self.s.survival.floor_usd:.2f}. Selling up.")
            self.store.set("status", DEAD)
            self.store.log("I've run out of life. Trading has stopped for good. "
                           "Run `python -m bot reset` to start a new life.", level="critical", ts=now)
            return

        day_start = self.store.get("day_start_equity")
        limit = self.s.survival.daily_loss_limit_pct / 100
        if self.status == RUNNING and equity <= day_start * (1 - limit):
            self._close(price, now, f"Down {(1 - equity / day_start) * 100:.1f}% today. Daily loss limit hit.")
            self.store.set("status", PAUSED)
            self.store.log("Sitting out for the rest of the UTC day.", level="warning", ts=now)
            return

        if self.status != RUNNING:
            return

        stop = self.store.get("stop")
        if self.qty > 0 and stop_hit(price, stop):
            self._close(price, now, f"Stop hit: price {price:,.2f} fell to or below stop {stop:,.2f}.")

        candles = self.market.candles(self.s.bot.interval_minutes)
        if not candles or candles[-1].ts == self.store.get("last_candle_ts"):
            return
        self.store.set("last_candle_ts", candles[-1].ts)

        decision = on_candle_close(candles, self.s.strategy, self.qty > 0, self.store.get("stop"))
        if decision.action == "buy":
            notional = position_size(equity, self.cash, price, decision.stop, self.s.strategy, self.s.costs.gas_per_swap_usd)
            if notional == 0:
                self.store.log(f"{decision.reason} But the trade would be too small, so skipping.", ts=now)
                return
            self._open(notional, price, decision.stop, now, decision.reason)
        elif decision.action == "sell":
            self._close(price, now, decision.reason)
        else:
            if decision.stop is not None:
                self.store.set("stop", decision.stop)
            self.store.log(f"New {self.s.bot.interval_minutes // 60}h candle closed at {candles[-1].close:,.2f}. {decision.reason}", ts=now)

    # Survival bookkeeping
    def _pay_running_costs(self, now: int) -> None:
        last = self.store.get("last_cost_ts")
        elapsed = now - last
        if elapsed < COST_INTERVAL:
            return
        cost = self.s.survival.daily_cost_usd * elapsed / 86400
        self.store.set("cash", self.cash - cost)
        self.store.set("last_cost_ts", now)
        self.store.add_ledger(now, "running_cost", -cost, f"Hosting for {elapsed / 3600:.1f}h")

    def _roll_day(self, now: int, equity: float) -> None:
        today = utc_day(now)
        if self.store.get("day") == today:
            return
        self.store.set("day", today)
        self.store.set("day_start_equity", equity)
        if self.status == PAUSED:
            self.store.set("status", RUNNING)
            self.store.log("New day. Daily loss limit reset, back to trading.", ts=now)

    # Trading
    def _open(self, notional: float, price: float, stop: float, now: int, reason: str) -> None:
        fill = self.broker.buy(notional, price)
        self.store.set("cash", self.cash + fill.cash_delta)
        self.store.set("qty", self.qty + fill.qty)
        self.store.set("cost_basis", -fill.cash_delta)
        self.store.set("stop", stop)
        self.store.add_trade(now, fill, None, self.market.usd_to_aud(), reason)
        self.store.log(f"BUY {fill.qty:.5f} {self.s.bot.asset} at {fill.price:,.2f} "
                       f"({fill.notional:.2f} USDT, fees {fill.fee + fill.gas:.2f}). {reason}", level="trade", ts=now)

    def _close(self, price: float, now: int, reason: str) -> None:
        if self.qty <= 0:
            return
        fill = self.broker.sell(self.qty, price)
        pnl = fill.cash_delta - self.store.get("cost_basis")
        self.store.set("cash", self.cash + fill.cash_delta)
        self.store.set("qty", 0.0)
        self.store.set("cost_basis", 0.0)
        self.store.set("stop", None)
        self.store.add_trade(now, fill, pnl, self.market.usd_to_aud(), reason)
        self.store.log(f"SELL {fill.qty:.5f} {self.s.bot.asset} at {fill.price:,.2f}. "
                       f"{'Profit' if pnl >= 0 else 'Loss'} {pnl:+.2f} USDT. {reason}", level="trade", ts=now)

    # Controls from the dashboard
    def kill(self) -> None:
        with self.lock:
            if self.status == DEAD:
                return
            now = int(self.clock())
            price = self.market.price()
            self._close(price, now, "Kill switch pressed.")
            self.store.set("status", STOPPED)
            self.store.log("Kill switch: everything sold, trading stopped until you press Resume.", level="warning", ts=now)

    def resume(self) -> None:
        with self.lock:
            if self.status in (STOPPED, PAUSED):
                self.store.set("status", RUNNING)
                self.store.log("Resumed by you.", ts=int(self.clock()))

    # Dashboard summary
    def summary(self) -> dict:
        price = self.last_price
        cash, qty = self.cash, self.qty
        equity = cash + qty * price if price else None
        start = self.s.bot.starting_balance
        costs_paid = -self.store.ledger_total("running_cost")
        realised = sum(t["pnl"] or 0 for t in self.store.trades())
        daily_cost = self.s.survival.daily_cost_usd
        runway = (equity - self.s.survival.floor_usd) / daily_cost if equity is not None else None
        stop = self.store.get("stop")
        cost_basis = self.store.get("cost_basis")
        return {
            "mode": self.s.bot.mode,
            "status": self.status,
            "pair": self.s.bot.pair,
            "asset": self.s.bot.asset,
            "interval_minutes": self.s.bot.interval_minutes,
            "price": price,
            "cash": cash,
            "qty": qty,
            "equity": equity,
            "starting_balance": start,
            "total_pnl": equity - start if equity is not None else None,
            "trading_pnl": realised + (qty * price - cost_basis if qty and price else 0),
            "costs_paid": costs_paid,
            "monthly_cost": self.s.survival.monthly_running_cost_usd,
            "floor": self.s.survival.floor_usd,
            "runway_days": max(runway, 0) if runway is not None else None,
            "position": None if not qty else {
                "qty": qty, "cost_basis": cost_basis, "stop": stop,
                "value": qty * price if price else None,
                "unrealised": qty * price - cost_basis if price else None,
            },
            "started_at": self.store.get("started_at"),
            "day_start_equity": self.store.get("day_start_equity"),
            "daily_loss_limit_pct": self.s.survival.daily_loss_limit_pct,
            "last_error": self.last_error,
        }
