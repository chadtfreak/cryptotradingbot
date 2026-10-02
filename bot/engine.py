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
    def __init__(self, settings: Settings, market, store: Store, broker=None, clock=time.time, brain=None, guardrails=None):
        self.s = settings
        self.g = guardrails or settings.guardrails
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
        self.store.log(f"Born with {bal:.2f} USDC in paper mode. Hosting costs "
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
        self._pay_funding(now, price)
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
        limit = self.g.daily_loss_limit_pct / 100
        if limit > 0 and self.status == RUNNING and equity <= day_start * (1 - limit):
            self._close(price, now, f"Down {(1 - equity / day_start) * 100:.1f}% today. Daily loss limit hit.")
            self.store.set("status", PAUSED)
            self.store.log("Sitting out for the rest of the UTC day.", level="warning", ts=now)
            return

        if self.status != RUNNING:
            return

        stop = self.store.get("stop")
        if self.qty != 0 and stop_hit(price, stop, self.qty):
            moved = "fell to or below" if self.qty > 0 else "rose to or above"
            self._close(price, now, f"Stop hit: price {price:,.2f} {moved} stop {stop:,.2f}.")

        candles = self.market.candles(self.s.bot.interval_minutes)
        decision = self.brain.decide(self, candles, price, now)
        if decision is not None:
            self.apply(decision, price, now)

    # Guardrails: every brain's decision goes through here
    def apply(self, d: Decision, price: float, now: int) -> None:
        if d.action in ("buy", "short"):
            self._apply_open(d, price, now)
        elif d.action == "sell":
            if self.qty != 0:
                g = self.g
                if g.max_trades_per_day and self.trades_today(now) >= g.max_trades_per_day and d.sell_fraction < 1:
                    self.store.log("Guardrail: trade limit reached, a partial close isn't allowed. Full exits always are.", level="warning", ts=now)
                    return
                self._close(price, now, d.reason, d.sell_fraction)
        elif d.stop is not None and self.qty != 0:
            self._move_stop(d.stop, price, now)

    def _apply_open(self, d: Decision, price: float, now: int) -> None:
        g = self.g
        side = 1 if d.action == "buy" else -1
        word = "buy" if side > 0 else "short"
        if side < 0 and not g.allow_short:
            self.store.log("Guardrail: this bot isn't allowed to short.", level="warning", ts=now)
            return
        if self.qty * side < 0:
            self._close(price, now, f"Flipping from {'long' if self.qty > 0 else 'short'} to {'long' if side > 0 else 'short'}. {d.reason}")
        elif self.qty != 0 and not g.allow_adding:
            self.store.log(f"Guardrail: already in a position, not adding to it. {d.reason}", ts=now)
            return
        if g.max_trades_per_day and self.trades_today(now) >= g.max_trades_per_day:
            self.store.log(f"Guardrail: already made {g.max_trades_per_day} trades today, skipping the {word}.", level="warning", ts=now)
            return
        lo, hi = g.min_stop_distance_pct / 100, g.max_stop_distance_pct / 100
        ok = d.stop is not None and (price * (1 - hi) <= d.stop <= price * (1 - lo) if side > 0 else price * (1 + lo) <= d.stop <= price * (1 + hi))
        if not ok:
            where = "below" if side > 0 else "above"
            self.store.log(f"Guardrail: {word} rejected, the stop must sit {g.min_stop_distance_pct:g}% to "
                           f"{g.max_stop_distance_pct:g}% {where} price (got {d.stop}).", level="warning", ts=now)
            return
        stop = self.store.get("stop")
        new_stop = d.stop
        if self.qty != 0 and stop is not None and g.stops_only_up:  # adding: keep whichever stop is tighter
            new_stop = max(stop, d.stop) if side > 0 else min(stop, d.stop)
        equity = self.equity(price)
        gas = self.s.costs.gas_per_swap_usd
        distance = abs(price - new_stop) / price
        if d.size_usd is None:
            notional = position_size(equity, self.cash, price, new_stop, self.s.strategy, gas)
        else:
            # Risk is what the whole position would lose if the stop is hit
            held_risk = abs(self.qty) * abs(price - new_stop)
            risk_cap = max(equity * g.max_risk_per_trade - held_risk, 0) / distance
            # No leverage: total exposure can't exceed equity
            room = equity * g.max_leverage * self.s.strategy.max_position_pct - abs(self.qty) * price - gas
            if side > 0:
                room = min(room, self.cash * self.s.strategy.max_position_pct - gas)
            notional = min(d.size_usd, risk_cap, room)
            if notional < d.size_usd - 0.01:
                self.store.log(f"Guardrail: trade trimmed from {d.size_usd:.2f} to {max(notional, 0):.2f} USDC "
                               f"to keep the risk under {g.max_risk_per_trade:.0%} of equity with no leverage.", ts=now)
            if notional < self.s.strategy.min_trade_usd:
                notional = 0.0
        if notional <= 0:
            self.store.log(f"{d.reason} But the trade would be too small, so skipping.", ts=now)
            return
        self._open(side, notional, price, new_stop, now, d.reason)

    def _move_stop(self, new: float, price: float, now: int) -> None:
        stop = self.store.get("stop")
        long = self.qty > 0
        loosening = stop is not None and (new < stop if long else new > stop)
        if self.g.stops_only_up and loosening:
            self.store.log(f"Guardrail: stops can only be tightened, keeping {stop:,.2f}.", ts=now)
        elif (long and new >= price) or (not long and new <= price):
            self.store.log(f"Guardrail: stop {new:,.2f} is on the wrong side of the price, ignored.", level="warning", ts=now)
        else:
            self.store.set("stop", new)

    # Survival bookkeeping
    def _pay_running_costs(self, now: int) -> None:
        last = self.store.get("last_cost_ts")
        elapsed = now - last
        if elapsed < COST_INTERVAL:
            return
        cost = self.s.survival.daily_cost_usd * elapsed / 86400
        self.store.set("last_cost_ts", now)
        self.charge(now, "running_cost", cost, f"Hosting for {elapsed / 3600:.1f}h")

    def _pay_funding(self, now: int, price: float) -> None:
        """Perp funding, settled hourly: longs pay shorts when the rate is positive."""
        last = self.store.get("last_funding_ts")
        if self.qty == 0 or last is None:
            self.store.set("last_funding_ts", now)
            return
        hours = (now - last) / 3600
        if hours < 1:
            return
        rate = self.market.funding_rate()
        self.store.set("last_funding_ts", now)
        if rate is None:
            return
        amount = self.qty * price * rate * hours
        self.charge(now, "funding", amount, f"Funding {rate * 100:.4f}%/h for {hours:.1f}h")

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
    def _open(self, side: int, notional: float, price: float, stop: float, now: int, reason: str) -> None:
        # Shorts sell ETH we don't own (a 1x perp): cash goes up and qty goes negative, so
        # equity = cash + qty * price holds for both sides.
        fill = self.broker.buy(notional, price) if side > 0 else self.broker.sell(notional / price, price)
        adding = self.qty != 0
        self.store.set("cash", self.cash + fill.cash_delta)
        self.store.set("qty", self.qty + side * fill.qty)
        self.store.set("cost_basis", (self.store.get("cost_basis") if adding else 0.0) - fill.cash_delta)
        self.store.set("stop", stop)
        self.store.add_trade(now, fill, None, self.market.usd_to_aud(), reason)
        label = ("ADD TO LONG" if adding else "BUY") if side > 0 else ("ADD TO SHORT" if adding else "SHORT")
        self.store.log(f"{label} {fill.qty:.5f} {self.s.bot.asset} at {fill.price:,.2f} "
                       f"({fill.notional:.2f} USDC, fees {fill.fee + fill.gas:.2f}), stop {stop:,.2f}. {reason}", level="trade", ts=now)

    def _close(self, price: float, now: int, reason: str, fraction: float = 1.0) -> None:
        if self.qty == 0:
            return
        fraction = min(max(fraction, 0.0), 1.0)
        if fraction < 1 and abs(self.qty) * (1 - fraction) * price < self.s.strategy.min_trade_usd:
            fraction = 1.0  # don't leave a dust position behind
        if fraction <= 0:
            return
        long = self.qty > 0
        qty = abs(self.qty) * fraction
        basis = self.store.get("cost_basis") * fraction
        fill = self.broker.sell(qty, price) if long else self.broker.buy_qty(qty, price)
        pnl = fill.cash_delta - basis
        self.store.set("cash", self.cash + fill.cash_delta)
        if fraction >= 1:
            self.store.set("qty", 0.0)
            self.store.set("cost_basis", 0.0)
            self.store.set("stop", None)
        else:
            self.store.set("qty", self.qty - qty if long else self.qty + qty)
            self.store.set("cost_basis", self.store.get("cost_basis") - basis)
        self.store.add_trade(now, fill, pnl, self.market.usd_to_aud(), reason)
        what = "SELL" if long else "BUY BACK"
        label = f"{what} {fraction:.0%} of position:" if fraction < 1 else ("SELL" if long else "CLOSE SHORT")
        self.store.log(f"{label} {fill.qty:.5f} {self.s.bot.asset} at {fill.price:,.2f}. "
                       f"{'Profit' if pnl >= 0 else 'Loss'} {pnl:+.2f} USDC. {reason}", level="trade", ts=now)

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
        funding = -self.store.ledger_total("funding")
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
            "trading_pnl": realised + (qty * price - cost_basis if qty and price else 0) - funding,
            "costs_paid": hosting + ai,
            "hosting_paid": hosting,
            "ai_paid": ai,
            "funding_paid": funding,
            "monthly_cost": self.s.survival.monthly_running_cost_usd,
            "daily_burn": burn,
            "floor": self.s.survival.floor_usd,
            "runway_days": max(runway, 0) if runway is not None else None,
            "buy_hold_pct": (price / start_price - 1) * 100 if price and start_price else None,
            "funding_rate": self.market.last_funding if hasattr(self.market, "last_funding") else None,
            "position": None if not qty else {
                "side": "long" if qty > 0 else "short",
                "qty": qty, "cost_basis": cost_basis, "stop": stop,
                "value": qty * price if price else None,
                "unrealised": qty * price - cost_basis if price else None,
            },
            "started_at": self.store.get("started_at"),
            "day_start_equity": self.store.get("day_start_equity"),
            "daily_loss_limit_pct": self.g.daily_loss_limit_pct,
            "style": self.g.style,
            "last_error": self.last_error,
            **self.brain.extra_summary(self),
        }
