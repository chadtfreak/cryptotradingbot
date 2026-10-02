"""The bot's body: wakes up, pays its bills, checks its health, enforces the survival
rules and executes trades. What to trade comes from a pluggable brain (maths or Claude),
but every decision passes through the guardrails here first."""

import threading
import time
from datetime import datetime, timezone

from .broker import PaperBroker
from .config import Settings
from .store import Store
from .strategy import Decision, on_candle_close, position_size, stop_hit

RUNNING, PAUSED, STOPPED, DEAD = "running", "paused", "stopped", "dead"
COST_INTERVAL = 3600  # charge running costs hourly
EQUITY_BUCKET = 300  # keep one equity point per 5 minutes


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def day_start_ts(ts: float) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


class MathsBrain:
    """The original EMA crossover strategy, deciding once per closed candle."""

    name = "maths"
    label = "Maths bot"

    def decide(self, eng: "Engine", candles, price: float, now: int) -> Decision | None:
        if not candles or candles[-1].ts == eng.store.get("last_candle_ts"):
            return None
        eng.store.set("last_candle_ts", candles[-1].ts)
        d = on_candle_close(candles, eng.s.strategy, eng.qty > 0, eng.store.get("stop"))
        if d.action == "hold":
            eng.store.log(f"New {eng.s.bot.interval_minutes // 60}h candle closed at {candles[-1].close:,.2f}. {d.reason}", ts=now)
        return d

    def extra_summary(self, eng: "Engine") -> dict:
        return {}


class Engine:
    def __init__(self, settings: Settings, market, store: Store, broker=None, clock=time.time, brain=None):
        self.s = settings
        self.market = market
        self.store = store
        self.broker = broker or PaperBroker(settings.costs)
        self.clock = clock
        self.brain = brain or MathsBrain()
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
        self.store.log(f"Born with {bal:.2f} USDT in paper mode. Hosting costs "
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

    def charge(self, now: int, kind: str, amount: float, note: str) -> None:
        """Take a running cost out of the bot's own balance."""
        self.store.set("cash", self.cash - amount)
        self.store.add_ledger(now, kind, -amount, note)

    def trades_today(self, now: int) -> int:
        start = day_start_ts(now)
        return sum(1 for t in self.store.trades(200) if t["ts"] >= start)

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
        if self.store.get("start_price") is None:
            self.store.set("start_price", price)
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
        decision = self.brain.decide(self, candles, price, now)
        if decision is not None:
            self.apply(decision, price, now)

    # Guardrails: every brain's decision goes through here
    def apply(self, d: Decision, price: float, now: int) -> None:
        g = self.s.guardrails
        stop = self.store.get("stop")
        if d.action == "buy":
            if self.qty > 0:
                self.store.log(f"Guardrail: already holding, not adding to the position. {d.reason}", ts=now)
                return
            if self.trades_today(now) >= g.max_trades_per_day:
                self.store.log(f"Guardrail: already made {g.max_trades_per_day} trades today, skipping the buy.", level="warning", ts=now)
                return
            if d.stop is None or not (price * (1 - g.max_stop_distance_pct / 100) <= d.stop <= price * (1 - g.min_stop_distance_pct / 100)):
                self.store.log(f"Guardrail: buy rejected, the stop must sit {g.min_stop_distance_pct}% to "
                               f"{g.max_stop_distance_pct}% below price (got {d.stop}).", level="warning", ts=now)
                return
            equity = self.equity(price)
            gas = self.s.costs.gas_per_swap_usd
            if d.size_usd is None:
                notional = position_size(equity, self.cash, price, d.stop, self.s.strategy, gas)
            else:
                risk_cap = equity * g.max_risk_per_trade / ((price - d.stop) / price)
                notional = min(d.size_usd, risk_cap, self.cash * self.s.strategy.max_position_pct - gas)
                if notional < d.size_usd - 0.01:
                    self.store.log(f"Guardrail: trade trimmed from {d.size_usd:.2f} to {notional:.2f} USDT "
                                   f"to keep the risk under {g.max_risk_per_trade:.0%} of equity.", ts=now)
                if notional < self.s.strategy.min_trade_usd:
                    notional = 0.0
            if notional == 0:
                self.store.log(f"{d.reason} But the trade would be too small, so skipping.", ts=now)
                return
            self._open(notional, price, d.stop, now, d.reason)
        elif d.action == "sell":
            if self.qty > 0:
                self._close(price, now, d.reason)
        elif d.stop is not None and self.qty > 0:
            if stop is not None and d.stop < stop:
                self.store.log(f"Guardrail: stops only move up, keeping {stop:,.2f}.", ts=now)
            elif d.stop >= price:
                self.store.log(f"Guardrail: stop {d.stop:,.2f} would be above the price, ignored.", level="warning", ts=now)
            else:
                self.store.set("stop", d.stop)

    # Survival bookkeeping
    def _pay_running_costs(self, now: int) -> None:
        last = self.store.get("last_cost_ts")
        elapsed = now - last
        if elapsed < COST_INTERVAL:
            return
        cost = self.s.survival.daily_cost_usd * elapsed / 86400
        self.store.set("last_cost_ts", now)
        self.charge(now, "running_cost", cost, f"Hosting for {elapsed / 3600:.1f}h")

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
    def daily_burn(self) -> float:
        """Average daily running cost: hosting plus the last week's thinking."""
        week_ago = int(self.clock()) - 7 * 86400
        ai_week = -self.store.ledger_total_since("ai_cost", week_ago)
        return self.s.survival.daily_cost_usd + ai_week / 7

    def summary(self) -> dict:
        price = self.last_price
        cash, qty = self.cash, self.qty
        equity = cash + qty * price if price else None
        start = self.s.bot.starting_balance
        hosting = -self.store.ledger_total("running_cost")
        ai = -self.store.ledger_total("ai_cost")
        realised = sum(t["pnl"] or 0 for t in self.store.trades(100000))
        burn = self.daily_burn()
        runway = (equity - self.s.survival.floor_usd) / burn if equity is not None and burn > 0 else None
        stop = self.store.get("stop")
        cost_basis = self.store.get("cost_basis")
        start_price = self.store.get("start_price")
        return {
            "bot": self.brain.name,
            "label": self.brain.label,
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
            "costs_paid": hosting + ai,
            "hosting_paid": hosting,
            "ai_paid": ai,
            "monthly_cost": self.s.survival.monthly_running_cost_usd,
            "daily_burn": burn,
            "floor": self.s.survival.floor_usd,
            "runway_days": max(runway, 0) if runway is not None else None,
            "buy_hold_pct": (price / start_price - 1) * 100 if price and start_price else None,
            "position": None if not qty else {
                "qty": qty, "cost_basis": cost_basis, "stop": stop,
                "value": qty * price if price else None,
                "unrealised": qty * price - cost_basis if price else None,
            },
            "started_at": self.store.get("started_at"),
            "day_start_equity": self.store.get("day_start_equity"),
            "daily_loss_limit_pct": self.s.survival.daily_loss_limit_pct,
            "last_error": self.last_error,
            **self.brain.extra_summary(self),
        }
