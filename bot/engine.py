"""The bot's body: wakes up, pays its bills, checks its health, enforces the survival
rules and executes trades. What to trade comes from a pluggable brain (maths or Claude),
but every decision passes through the guardrails here first.

A bot can hold several positions, one per coin. Each is a 1x perp position: a long holds
coins bought with cash, a short sells coins it doesn't own (cash goes up, qty goes negative),
so equity = cash + sum(qty * price) holds for any mix of longs and shorts."""

import dataclasses
import threading
import time
from datetime import datetime, timezone

from .broker import Fill, PaperBroker
from .config import Settings
from .store import Store
from .strategy import Decision, on_candle_close, position_size, stop_hit


def is_blip(exc: Exception) -> bool:
    """A network hiccup or exchange outage rather than a bug."""
    import httpx

    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (429, 500, 502, 503, 504)


def short(exc: Exception) -> str:
    import httpx

    return f"error {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__

RUNNING, PAUSED, STOPPED, DEAD = "running", "paused", "stopped", "dead"
COST_INTERVAL = 3600  # charge running costs hourly
EQUITY_BUCKET = 300  # keep one equity point per 5 minutes


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def day_start_ts(ts: float) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def fmt_px(p: float) -> str:
    return f"{p:,.2f}" if p >= 1 else f"{p:.6g}"


class MathsBrain:
    """The original EMA crossover strategy on the primary coin, deciding once per closed candle."""

    name = "maths"
    label = "Maths bot"

    def decide(self, eng: "Engine", candles, price: float, now: int) -> Decision | None:
        if not candles or candles[-1].ts == eng.store.get("last_candle_ts"):
            return None
        eng.store.set("last_candle_ts", candles[-1].ts)
        pos = eng.position()
        d = on_candle_close(candles, eng.s.strategy, eng.qty > 0, pos["stop"] if pos else None)
        if d.action == "hold":
            eng.store.log(f"New {eng.s.bot.interval_minutes // 60}h candle closed at {candles[-1].close:,.2f}. {d.reason}", ts=now)
        return d

    def extra_summary(self, eng: "Engine") -> dict:
        return {}


class Engine:
    def __init__(self, settings: Settings, market, store: Store, broker=None, clock=time.time, brain=None, guardrails=None):
        self.s = settings
        self.base_g = guardrails or settings.guardrails
        self.primary = settings.bot.asset
        self.market = market
        self.store = store
        self.broker = broker or PaperBroker(settings.costs)
        self.clock = clock
        self.brain = brain or MathsBrain()
        self.live = getattr(self.broker, "live", False)
        self.retired = False  # set when replaced by a venue switch
        self.lock = threading.Lock()
        self.prices: dict[str, float] = {}
        self.last_error: str | None = None
        self.failures = 0  # ticks failed in a row
        self._leverage_set: set[str] = set()
        self._init_account()

    def _init_account(self) -> None:
        if self.store.get("cash") is not None:
            if self.store.get("positions") is None:  # migrate a single-position database
                qty = self.store.get("qty") or 0.0
                self.store.set("positions", {} if not qty else {self.primary: {
                    "qty": qty, "cost_basis": self.store.get("cost_basis") or 0.0, "stop": self.store.get("stop")}})
            return
        now = int(self.clock())
        bal = self.s.bot.starting_balance
        for key, value in {
            "cash": bal, "positions": {}, "status": RUNNING,
            "started_at": now, "last_cost_ts": now, "last_candle_ts": None,
            "day": utc_day(now), "day_start_equity": bal,
        }.items():
            self.store.set(key, value)
        self.store.add_ledger(now, "deposit", bal, "Starting balance")
        self.store.log(f"Born with {bal:.2f} USDC. Hosting costs "
                       f"{self.s.survival.monthly_running_cost_usd:.2f} USD a month, paid from my own balance.", ts=now)

    # Guardrails, tightened while on probation
    @property
    def g(self):
        g = self.base_g
        if self.store.get("probation"):
            g = dataclasses.replace(g, max_risk_per_trade=g.max_risk_per_trade / 2, max_positions=1)
        if g.drawdown_half_risk_pct and (self.store.get("drawdown") or 0) * 100 >= g.drawdown_half_risk_pct:
            g = dataclasses.replace(g, max_risk_per_trade=g.max_risk_per_trade / 2)
        return g

    # Drawdown: how far below its best it is, measured like a fund's unit price so top-ups don't count
    def _nav(self, equity: float) -> float:
        units = self.store.get("units")
        if not units:
            units = equity  # start measuring from here: unit price 1.0
            self.store.set("units", units)
        return equity / units

    def _track_drawdown(self, now: int, equity: float) -> None:
        g = self.base_g
        if not (g.drawdown_half_risk_pct or g.drawdown_pause_pct) or equity <= 0:
            return
        nav = self._nav(equity)
        peak = max(self.store.get("nav_peak") or nav, nav)
        dd = 1 - nav / peak
        was = self.store.get("drawdown") or 0
        self.store.set("nav_peak", peak)
        self.store.set("drawdown", dd)
        self.store.set("max_drawdown", max(self.store.get("max_drawdown") or 0, dd))
        half = g.drawdown_half_risk_pct / 100
        if half and was < half <= dd:
            self.store.log(f"Down {dd * 100:.0f}% from my best. Halving my risk per trade until I make a new high.", level="warning", ts=now)
        if half and was >= half and dd == 0:
            self.store.log("New high. Full risk per trade restored.", ts=now)
        pause = g.drawdown_pause_pct / 100
        if pause and dd >= pause and self.store.get("dd_pause_peak") != peak:
            self.store.set("dd_pause_peak", peak)
            self.store.set("dd_pause_until", now + int(g.drawdown_pause_hours * 3600))
            self.store.log(f"Down {dd * 100:.0f}% from my best. No new trades for {g.drawdown_pause_hours:.0f} hours: step back, "
                           "then come back at half risk. Stops and exits still work.", level="warning", ts=now)

    # Account helpers
    @property
    def cash(self) -> float:
        return self.store.get("cash")

    @property
    def status(self) -> str:
        return self.store.get("status")

    @property
    def positions(self) -> dict[str, dict]:
        return self.store.get("positions") or {}

    def position(self, coin: str | None = None) -> dict | None:
        return self.positions.get(coin or self.primary)

    def _save_position(self, coin: str, pos: dict | None) -> None:
        positions = self.positions
        if pos is None or pos["qty"] == 0:
            positions.pop(coin, None)
        else:
            positions[coin] = pos
        self.store.set("positions", positions)

    @property
    def qty(self) -> float:
        """Quantity held of the primary coin (signed)."""
        pos = self.position()
        return pos["qty"] if pos else 0.0

    @property
    def last_price(self) -> float | None:
        return self.prices.get(self.primary)

    @property
    def contributed(self) -> float:
        """Money put in: the starting balance plus any promotions."""
        return self.store.ledger_total("deposit") or self.s.bot.starting_balance

    @property
    def bankroll_base(self) -> float:
        """The bankroll the owner set: the starting balance plus top-ups, not counting promotions."""
        return self.store.get("bankroll_base") or self.s.bot.starting_balance

    @property
    def floor(self) -> float:
        """The survival floor. It scales with the bankroll the owner sets (half of it by default)."""
        return self.store.get("floor_usd") or self.s.survival.floor_usd

    def available_to_add(self) -> float | None:
        """On a live venue, how much of the exchange account isn't already part of the bankroll."""
        value = self.store.get("account_value")
        if not self.live or value is None or not self.prices:
            return None
        return max(value - self.equity(), 0.0)

    def top_up(self, now: int, amount: float) -> None:
        """The owner adds to the bankroll. The floor moves with it, so the survival rule stays the same."""
        if amount <= 0:
            raise ValueError("Choose an amount above zero.")
        if self.live:
            if (self.store.get("venue") or {}).get("name") == "mainnet":
                raise ValueError("On real money, top-ups aren't supported from the dashboard yet.")
            room = self.available_to_add()
            if room is None or amount > room + 0.01:
                raise ValueError(f"The exchange account only has {room or 0:,.2f} USDC that isn't already in the bankroll.")
        ratio = self.s.survival.floor_usd / self.s.bot.starting_balance
        base = self.bankroll_base + amount
        self.deposit(now, amount, "Owner top-up")
        self.store.set("bankroll_base", base)
        self.store.set("floor_usd", round(base * ratio, 2))
        self.store.log(f"My owner added {amount:,.2f} USDC to my bankroll, now {base:,.2f}. "
                       f"My survival floor moves to {base * ratio:,.2f}.", level="trade", ts=now)

    def beta(self, coin: str) -> float | None:
        """How much a coin moves for each 1% BTC moves, from the last 20 days of 4h candles. Cached hourly."""
        if coin == "BTC":
            return 1.0
        key = (coin, int(self.clock()) // 3600)
        cache = self.__dict__.setdefault("_betas", {})
        if key in cache:
            return cache[key]
        try:
            a = [c.close for c in self.market.candles(240, coin=coin, count=121)]
            b = [c.close for c in self.market.candles(240, coin="BTC", count=121)]
            n = min(len(a), len(b))
            ra = [a[i] / a[i - 1] - 1 for i in range(len(a) - n + 1, len(a))]
            rb = [b[i] / b[i - 1] - 1 for i in range(len(b) - n + 1, len(b))]
            ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
            var = sum((x - mb) ** 2 for x in rb)
            beta = sum((x - mb) * (y - ma) for x, y in zip(rb, ra)) / var if var else None
        except Exception:
            beta = None
        cache[key] = beta
        return beta

    def btc_exposure(self, exclude: str | None = None) -> float:
        """What all positions add up to as a bet on BTC, in USDC (unknown links count as 1)."""
        total = 0.0
        for c, p in self.positions.items():
            if c != exclude:
                b = self.beta(c)
                total += p["qty"] * self.prices.get(c, 0) * (1.0 if b is None else b)
        return total

    def open_risk(self, exclude: str | None = None) -> float:
        """What every open trade would lose against its entry if all stops were hit. A trade whose
        stop is at breakeven or better risks nothing, which frees room for new trades."""
        total = 0.0
        for coin, p in self.positions.items():
            if coin == exclude or not p.get("stop") or not p["qty"]:
                continue
            entry = p.get("entry_px") or abs(p["cost_basis"] / p["qty"])
            total += max((entry - p["stop"]) * p["qty"], 0.0) if p["qty"] > 0 else max((p["stop"] - entry) * -p["qty"], 0.0)
        return total

    def exposure(self, prices: dict | None = None) -> float:
        prices = prices or self.prices
        return sum(abs(p["qty"]) * prices.get(c, 0) for c, p in self.positions.items())

    def equity(self, price: float | dict | None = None) -> float:
        prices = dict(self.prices)
        if isinstance(price, dict):
            prices.update(price)
        elif price is not None:
            prices[self.primary] = price
        return self.cash + sum(p["qty"] * prices.get(c, 0) for c, p in self.positions.items())

    def charge(self, now: int, kind: str, amount: float, note: str) -> None:
        """Take a running cost out of the bot's own balance."""
        self.store.set("cash", self.cash - amount)
        self.store.add_ledger(now, kind, -amount, note)

    def deposit(self, now: int, amount: float, note: str) -> None:
        units = self.store.get("units")
        if units and self.prices:  # new money buys units at today's price, so it doesn't look like a gain
            self.store.set("units", units + amount / (self.equity() / units))
        self.store.set("cash", self.cash + amount)
        self.store.add_ledger(now, "deposit", amount, note)

    def trades_today(self, now: int) -> int:
        start = day_start_ts(now)
        return sum(1 for t in self.store.trades(500) if t["ts"] >= start)

    def _fetch_prices(self) -> dict[str, float]:
        src = self.broker.venue if self.live else self.market
        if hasattr(src, "mids"):
            return src.mids()
        return {self.primary: src.price()}

    # Main loop step
    def tick(self) -> None:
        with self.lock:
            if self.retired:
                return
            try:
                self._tick()
                if self.failures >= 3:
                    self.store.log("Connection is back. Carrying on.")
                self.failures, self.last_error = 0, None
            except Exception as exc:  # network blips etc. Log and try again next poll.
                self.failures += 1
                if self.failures < 3 and is_blip(exc):
                    return  # a brief outage at the exchange: stops are on the exchange, just try again next minute
                if self.failures == 3 and is_blip(exc):
                    self.last_error = f"Can't reach Hyperliquid's market data ({exc}). Retrying every minute."
                    self.store.log(f"Can't reach Hyperliquid for the last few minutes ({short(exc)}). Retrying every minute.", level="warning")
                elif not is_blip(exc):
                    self.last_error = str(exc)
                    self.store.log(f"Tick failed: {exc}", level="error")

    def _tick(self) -> None:
        now = int(self.clock())
        self.prices = self._fetch_prices()
        price = self.prices[self.primary]
        if self.live and self.status != DEAD:
            self._reconcile(now)
        if self.store.get("start_price") is None:
            self.store.set("start_price", price)
        if self.status == DEAD:
            return

        self._pay_running_costs(now)
        if not self.live:  # on a real exchange, funding is already in the account value
            self._pay_funding(now)
        equity = self.equity()
        self._roll_day(now, equity)
        self._track_drawdown(now, equity)
        self.store.add_equity(now - now % EQUITY_BUCKET, equity, self.cash, self.qty, price)

        if equity < self.floor:
            self._close_all(now, f"Equity {equity:.2f} fell below the survival floor of {self.floor:.2f}. Selling up.")
            self.store.set("status", DEAD)
            self.store.log("I've run out of life. Trading has stopped for good. "
                           "Run `python -m bot reset` to start a new life.", level="critical", ts=now)
            return

        day_start = self.store.get("day_start_equity")
        limit = self.g.daily_loss_limit_pct / 100
        if limit > 0 and self.status == RUNNING and equity <= day_start * (1 - limit):
            self._close_all(now, f"Down {(1 - equity / day_start) * 100:.1f}% today. Daily loss limit hit.")
            self.store.set("status", PAUSED)
            self.store.log("Sitting out for the rest of the UTC day.", level="warning", ts=now)
            return

        if self.status != RUNNING:
            return

        for coin in list(self.positions):
            self._track(coin)
        exchange_stops = self.store.get("exchange_stops") or {}
        for coin, pos in list(self.positions.items()):
            p = self.prices.get(coin)
            if p is None or (self.live and exchange_stops.get(coin)):
                continue
            if stop_hit(p, pos.get("stop"), pos["qty"]):
                moved = "fell to or below" if pos["qty"] > 0 else "rose to or above"
                self._close(coin, now, f"Stop hit: {coin} {fmt_px(p)} {moved} stop {fmt_px(pos['stop'])}.")

        if self.g.manage_trades:
            for coin in list(self.positions):
                try:
                    self._manage(coin, now)
                except Exception as exc:  # e.g. the exchange rejects an order: the exchange stop still protects it
                    warned = self.store.get("manage_warned") or {}
                    if now - warned.get(coin, 0) >= 3600:
                        warned[coin] = now
                        self.store.set("manage_warned", warned)
                        self.store.log(f"Trade management for {coin} couldn't act ({exc}). Its stop is still in place; I'll keep trying.",
                                       level="warning", ts=now)
                    if not self.live:
                        raise

        candles = self.market.candles(self.s.bot.interval_minutes)
        decisions = self.brain.decide(self, candles, price, now)
        if decisions is None:
            return
        for d in decisions if isinstance(decisions, list) else [decisions]:
            self.apply(d, now)

    # Guardrails: every brain's decision goes through here
    def apply(self, d: Decision, now: int, price: float | None = None) -> None:
        if price is not None:
            self.prices[d.coin or self.primary] = price
        try:
            self._apply(d, now)
        except Exception as exc:
            if not self.live:
                raise
            self.store.log(f"The exchange rejected the trade: {exc}", level="error", ts=now)

    def _apply(self, d: Decision, now: int) -> None:
        coin = d.coin or self.primary
        pos = self.position(coin)
        if d.action in ("buy", "short"):
            self._apply_open(d, coin, now)
        elif d.action == "sell":
            if pos:
                g = self.g
                if g.max_trades_per_day and self.trades_today(now) >= g.max_trades_per_day and d.sell_fraction < 1:
                    self.store.log("Guardrail: trade limit reached, a partial close isn't allowed. Full exits always are.", level="warning", ts=now)
                    return
                self._close(coin, now, d.reason, d.sell_fraction)
        elif d.action == "target" and pos:
            self._set_target(coin, d.target, now)
        elif d.stop is not None and pos:
            self._move_stop(coin, d.stop, now)

    def tradable(self, coin: str) -> bool:
        if coin == self.primary:
            return True
        if not self.g.min_volume_usd or not hasattr(self.market, "liquid"):
            return False
        try:
            return coin in self.market.liquid(self.g.min_volume_usd)
        except Exception:
            return False

    def _book_problem(self, coin: str) -> str | None:
        """On a live venue, checks the exchange's order book can take the order before sending it."""
        venue = getattr(self.broker, "venue", None)
        if not self.live or not hasattr(venue, "book_problem"):
            return None
        try:
            real = self.market.mids().get(coin) if hasattr(self.market, "mids") else None
            return venue.book_problem(coin, real)
        except Exception:
            return None  # can't check: let the order try

    def _apply_open(self, d: Decision, coin: str, now: int) -> None:
        g = self.g
        side = 1 if d.action == "buy" else -1
        word = "buy" if side > 0 else "short"
        price = self.prices.get(coin)
        if price is None:
            self.store.log(f"Guardrail: no price for {coin}, skipping the {word}.", level="warning", ts=now)
            return
        if not self.tradable(coin):
            self.store.log(f"Guardrail: {coin} isn't on the list of liquid coins this bot may trade.", level="warning", ts=now)
            return
        if side < 0 and not g.allow_short:
            self.store.log("Guardrail: this bot isn't allowed to short.", level="warning", ts=now)
            return
        if now < (self.store.get("dd_pause_until") or 0):
            left = (self.store.get("dd_pause_until") - now) / 3600
            self.store.log(f"Guardrail: no new trades for another {left:.0f}h after a {g.drawdown_pause_pct:g}% drawdown. Skipping the {coin} {word}.",
                           level="warning", ts=now)
            return
        problem = self._book_problem(coin)
        if problem:
            bad = self.store.get("untradeable") or {}
            bad[coin] = {"why": problem, "ts": now}
            self.store.set("untradeable", bad)
            self.store.log(f"Can't {word} {coin} on this venue right now: {problem}. Skipping it.", level="warning", ts=now)
            return
        pos = self.position(coin)
        if pos and pos["qty"] * side < 0:
            self._close(coin, now, f"Flipping {coin} from {'long' if pos['qty'] > 0 else 'short'} to {'long' if side > 0 else 'short'}. {d.reason}")
            pos = None
        elif pos and not g.allow_adding:
            self.store.log(f"Guardrail: already in {coin}, not adding to it. {d.reason}", ts=now)
            return
        if not pos and len(self.positions) >= g.max_positions:
            self.store.log(f"Guardrail: already holding {len(self.positions)} position(s), the most allowed. Skipping the {coin} {word}.", level="warning", ts=now)
            return
        if g.max_trades_per_day and self.trades_today(now) >= g.max_trades_per_day:
            self.store.log(f"Guardrail: already made {g.max_trades_per_day} trades today, skipping the {word}.", level="warning", ts=now)
            return
        lo, hi = g.min_stop_distance_pct / 100, g.max_stop_distance_pct / 100
        ok = d.stop is not None and (price * (1 - hi) <= d.stop <= price * (1 - lo) if side > 0 else price * (1 + lo) <= d.stop <= price * (1 + hi))
        if not ok:
            where = "below" if side > 0 else "above"
            self.store.log(f"Guardrail: {coin} {word} rejected, the stop must sit {g.min_stop_distance_pct:g}% to "
                           f"{g.max_stop_distance_pct:g}% {where} price (got {d.stop}).", level="warning", ts=now)
            return
        new_stop = d.stop
        if pos and pos.get("stop") is not None and g.stops_only_up:  # adding: keep whichever stop is tighter
            new_stop = max(pos["stop"], d.stop) if side > 0 else min(pos["stop"], d.stop)
        equity = self.equity()
        gas = self.s.costs.gas_per_swap_usd
        distance = abs(price - new_stop) / price
        if d.size_usd is None:
            notional = position_size(equity, self.cash, price, new_stop, self.s.strategy, gas)
            notional = min(notional, max(equity * g.max_leverage * self.s.strategy.max_position_pct - self.exposure() - gas, 0))
        else:
            # Risk is what this coin's whole position would lose if its stop is hit
            held_risk = abs(pos["qty"]) * abs(price - new_stop) if pos else 0.0
            allowed = equity * g.max_risk_per_trade
            if g.max_total_risk:  # and every open trade together can't risk more than the portfolio cap
                allowed = min(allowed, equity * g.max_total_risk - self.open_risk(exclude=coin))
            risk_cap = max(allowed - held_risk, 0) / distance
            # No leverage: total exposure across every position can't exceed equity
            room = equity * g.max_leverage * self.s.strategy.max_position_pct - self.exposure() - gas
            notional = min(d.size_usd, risk_cap, room)
            if notional < d.size_usd - 0.01:
                self.store.log(f"Guardrail: {coin} trade trimmed from {d.size_usd:.2f} to {max(notional, 0):.2f} USDC "
                               f"to keep the risk under {g.max_risk_per_trade:.0%} of equity per trade"
                               + (f" and {g.max_total_risk:.0%} across all open trades" if g.max_total_risk else "") + ", with no leverage.", ts=now)
        if g.max_btc_exposure and notional > 0:
            beta = self.beta(coin)
            beta = 1.0 if beta is None else beta
            if abs(beta) > 0.05:
                held = self.btc_exposure(exclude=coin) + (pos["qty"] * price * beta if pos else 0.0)
                sign = 1 if side * beta > 0 else -1
                room = max(g.max_btc_exposure * equity - sign * held, 0) / abs(beta)
                if room < notional - 0.01:
                    self.store.log(f"Guardrail: {coin} trade trimmed from {notional:.2f} to {room:.2f} USDC so all positions together "
                                   f"don't act like more than {g.max_btc_exposure:.0%} of equity in BTC "
                                   f"({'long' if sign > 0 else 'short'} side, {coin} moves about {beta:.1f}x BTC).", ts=now)
                    notional = room
        if notional < self.s.strategy.min_trade_usd:
            self.store.log(f"{d.reason} But the {coin} trade would be too small, so skipping.", ts=now)
            return
        self._open(coin, side, notional, price, new_stop, now, d.reason, d.setup, d.target)

    def _move_stop(self, coin: str, new: float, now: int) -> None:
        pos = self.position(coin)
        price = self.prices.get(coin)
        stop, long = pos.get("stop"), pos["qty"] > 0
        loosening = stop is not None and (new < stop if long else new > stop)
        if self.g.stops_only_up and loosening:
            self.store.log(f"Guardrail: stops can only be tightened, keeping {coin} at {fmt_px(stop)}.", ts=now)
        elif price is None or (long and new >= price) or (not long and new <= price):
            self.store.log(f"Guardrail: {coin} stop {fmt_px(new)} is on the wrong side of the price, ignored.", level="warning", ts=now)
        else:
            pos["stop"] = new
            self._save_position(coin, pos)
            self._sync_stop(coin, now)

    # Trade management: what a professional sets up the moment a trade is on
    def _manage(self, coin: str, now: int) -> None:
        pos, p = self.position(coin), self.prices.get(coin)
        if not pos or p is None:
            return
        long = pos["qty"] > 0
        d = 1 if long else -1
        if not pos.get("risk_unit"):  # a position from before trade management: measure from here
            if not pos.get("stop"):
                return
            entry = abs(pos["cost_basis"] / pos["qty"])
            pos.update(entry_px=entry, risk_unit=abs(entry - pos["stop"]), best=p, worst=p, managed_since=now)
            pos.setdefault("target", entry + d * self.g.target_r * pos["risk_unit"])
        entry, unit = pos["entry_px"], pos["risk_unit"]
        gain = d * (p - entry) / unit if unit else 0.0

        if not pos.get("be_done") and gain >= 1:
            pos["be_done"] = True
            self._save_position(coin, pos)
            if pos.get("stop") is None or d * (entry - pos["stop"]) > 0:
                self._move_stop(coin, entry, now)
                self.store.log(f"{coin} is up {gain:.1f}R, so I moved its stop to breakeven ({fmt_px(entry)}). Worst case now is about the fees.", ts=now)
        target = pos.get("target")
        if target and not pos.get("half_taken") and d * (p - target) >= 0:
            pos["half_taken"] = True
            self._save_position(coin, pos)
            self._close(coin, now, f"Take profit: {coin} reached its target {fmt_px(target)} (+{gain:.1f}R). Banking half and trailing the rest.", 0.5)
            pos = self.position(coin)
            if not pos:
                return
        if pos.get("half_taken"):
            trail = pos["best"] - d * unit
            if pos.get("stop") is None or d * (trail - pos["stop"]) >= 0.25 * unit:
                self._move_stop(coin, trail, now)
        if not pos.get("be_done") and now - (pos.get("managed_since") or now) >= self.g.time_stop_hours * 3600:
            self._close(coin, now, f"Time stop: {coin} hasn't reached +1R in {self.g.time_stop_hours / 24:.0f} days ({gain:+.1f}R now). "
                                   "Freeing the money for something that's working.")

    def _track(self, coin: str) -> None:
        """Best and worst price since entry, for trailing stops and the movement stats."""
        pos, p = self.position(coin), self.prices.get(coin)
        if not pos or p is None or pos.get("entry_px") is None:
            return
        d = 1 if pos["qty"] > 0 else -1
        changed = False
        if d * (p - pos.get("best", pos["entry_px"])) > 0:
            pos["best"], changed = p, True
        if d * (pos.get("worst", pos["entry_px"]) - p) > 0:
            pos["worst"], changed = p, True
        if changed:
            self._save_position(coin, pos)

    def _set_target(self, coin: str, target: float | None, now: int) -> None:
        pos, p = self.position(coin), self.prices.get(coin)
        d = 1 if pos["qty"] > 0 else -1
        if target is not None and p is not None and d * (target - p) <= 0:
            self.store.log(f"Guardrail: {coin} target {fmt_px(target)} is on the wrong side of the price, ignored.", level="warning", ts=now)
            return
        pos["target"] = target
        pos["managed_since"] = now  # a fresh plan restarts the time stop
        self._save_position(coin, pos)
        self.store.log(f"{coin} take-profit " + (f"set to {fmt_px(target)}." if target else "removed: letting it run on the stop."), ts=now)

    # Survival bookkeeping
    def _pay_running_costs(self, now: int) -> None:
        last = self.store.get("last_cost_ts")
        elapsed = now - last
        if elapsed < COST_INTERVAL:
            return
        cost = self.s.survival.daily_cost_usd * elapsed / 86400
        self.store.set("last_cost_ts", now)
        self.charge(now, "running_cost", cost, f"Hosting for {elapsed / 3600:.1f}h")

    def _pay_funding(self, now: int) -> None:
        """Perp funding, settled hourly: longs pay shorts when the rate is positive."""
        last = self.store.get("last_funding_ts")
        if not self.positions or last is None:
            self.store.set("last_funding_ts", now)
            return
        hours = (now - last) / 3600
        if hours < 1:
            return
        self.store.set("last_funding_ts", now)
        for coin, pos in self.positions.items():
            rate = self.market.funding_rate(coin)
            price = self.prices.get(coin)
            if rate is None or price is None:
                continue
            self.charge(now, "funding", pos["qty"] * price * rate * hours, f"{coin} funding {rate * 100:.4f}%/h for {hours:.1f}h")

    def _roll_day(self, now: int, equity: float) -> None:
        today = utc_day(now)
        if self.store.get("day") == today:
            return
        self.store.set("day", today)
        self.store.set("day_start_equity", equity)
        if self.status == PAUSED:
            self.store.set("status", RUNNING)
            self.store.log("New day. Daily loss limit reset, back to trading.", ts=now)

    # Live exchange
    def _sync_stop(self, coin: str, now: int) -> None:
        """Mirror the bot's stop for a coin as a real stop order on the exchange."""
        if not self.live:
            return
        pos = self.position(coin)
        stops = self.store.get("exchange_stops") or {}
        try:
            self.broker.sync_stop(pos["qty"] if pos else 0.0, pos.get("stop") if pos else None, coin=coin)
            stops[coin] = bool(pos and pos.get("stop") is not None)
        except Exception as exc:
            stops[coin] = False
            self.store.log(f"Couldn't place the {coin} stop order on the exchange ({exc}). The bot will watch it itself.", level="error", ts=now)
        self.store.set("exchange_stops", stops)

    def _ensure_leverage(self, coin: str, now: int) -> None:
        if not self.live or coin in self._leverage_set:
            return
        self._leverage_set.add(coin)
        try:
            self.broker.venue.set_leverage(1, coin)
            self.store.log(f"Set the exchange's leverage for {coin} to 1x, so it will refuse anything bigger.", ts=now)
        except Exception as exc:
            self.store.log(f"Couldn't set the exchange's {coin} leverage to 1x ({exc}). The bot's own 1x limit still applies.", level="warning", ts=now)

    def _reconcile(self, now: int) -> None:
        """The exchange is the truth: rebuild balance and positions from it every tick.

        Equity is the money put in plus whatever the exchange account has gained or lost
        since the bot went live, minus the hosting and thinking it owes."""
        st = self.broker.venue.state()
        self.store.set("account_value", st.account_value)
        live_positions = {c: (q, e) for c, (q, e) in st.positions.items() if abs(q) * self.prices.get(c, e or 0) >= 1.0}
        for coin in live_positions:
            self._ensure_leverage(coin, now)
        if self.store.get("live_base") is None:
            self.store.set("live_base", st.account_value)
            self.store.set("live_started_at", now)
            self.store.log(f"Connected to Hyperliquid {self.broker.venue.network}. The account holds {st.account_value:,.2f} USDC; "
                           f"I'll trade it as a {self.contributed:.0f} USDC bankroll.", ts=now)
        start = self.store.get("live_started_at")
        owed = -(self.store.ledger_total_since("running_cost", start) + self.store.ledger_total_since("ai_cost", start))
        equity = self.contributed + (st.account_value - self.store.get("live_base")) - owed

        positions = self.positions
        for coin, pos in list(positions.items()):
            if coin not in live_positions:
                price = self.prices.get(coin, 0)
                pnl = pos["qty"] * price - pos["cost_basis"]
                fill = Fill("sell" if pos["qty"] > 0 else "buy", abs(pos["qty"]), price, abs(pos["qty"]) * price, 0.0, 0.0)
                self.store.add_trade(now, fill, pnl, self.market.usd_to_aud(), "Closed on the exchange (stop order fired or closed by hand).", coin=coin)
                self.store.log(f"My {coin} position was closed on the exchange, about {pnl:+.2f} USDC. Probably the stop order fired.", level="trade", ts=now)
                if hasattr(self.brain, "on_close"):
                    self.brain.on_close(self, coin, pos, pnl, 1.0, now, price, "Closed on the exchange, probably the stop order fired.")
                positions.pop(coin)
                self.broker.sync_stop(0, None, coin=coin)
                stops = self.store.get("exchange_stops") or {}
                stops.pop(coin, None)
                self.store.set("exchange_stops", stops)
        for coin, (q, entry) in live_positions.items():
            old = positions.get(coin)
            if old is None or abs(old["qty"] - q) > 1e-9:
                if old is None or old["qty"] * q < 0 or abs(old["qty"] - q) > 1e-6:
                    self.store.log(f"Synced my {coin} position from the exchange: {(old or {}).get('qty', 0):+.6g} to {q:+.6g}.", level="warning", ts=now)
            positions[coin] = {**(old or {}), "qty": q, "cost_basis": q * entry, "stop": (old or {}).get("stop")}
        self.store.set("positions", positions)
        self.store.set("cash", equity - sum(p["qty"] * self.prices.get(c, 0) for c, p in positions.items()))

    # Trading
    def _open(self, coin: str, side: int, notional: float, price: float, stop: float, now: int, reason: str,
              setup: str | None = None, target: float | None = None) -> None:
        self._ensure_leverage(coin, now)
        fill = self.broker.buy(notional, price, coin=coin) if side > 0 else self.broker.sell(notional / price, price, coin=coin)
        pos = self.position(coin)
        adding = pos is not None
        pos = pos or {"qty": 0.0, "cost_basis": 0.0, "opened_at": now, "setup": setup}
        if adding and pos.get("entry_px"):  # average entry across the adds
            held = abs(pos["qty"])
            pos["entry_px"] = (pos["entry_px"] * held + fill.price * fill.qty) / (held + fill.qty)
        pos["qty"] += side * fill.qty
        pos["cost_basis"] -= fill.cash_delta
        pos["stop"] = stop
        pos["risk_usd"] = (pos.get("risk_usd") or 0.0) + fill.qty * abs(fill.price - stop)  # so results can be measured in R
        if not adding:  # what trade management and the movement stats measure from
            pos.update(entry_px=fill.price, risk_unit=abs(fill.price - stop), best=fill.price, worst=fill.price, managed_since=now)
            if self.g.manage_trades:
                default = fill.price + side * self.g.target_r * abs(fill.price - stop)
                pos["target"] = target if target and (target - fill.price) * side > 0 else default
        elif target and (target - fill.price) * side > 0:
            pos["target"] = target
        self.store.set("cash", self.cash + fill.cash_delta)
        self._save_position(coin, pos)
        self.store.add_trade(now, fill, None, self.market.usd_to_aud(), reason, coin=coin)
        self._sync_stop(coin, now)
        label = ("ADD TO LONG" if adding else "BUY") if side > 0 else ("ADD TO SHORT" if adding else "SHORT")
        self.store.log(f"{label} {fill.qty:.6g} {coin} at {fmt_px(fill.price)} "
                       f"({fill.notional:.2f} USDC, fees {fill.fee + fill.gas:.2f}), stop {fmt_px(stop)}. {reason}", level="trade", ts=now)

    def _close(self, coin: str, now: int, reason: str, fraction: float = 1.0) -> None:
        pos = self.position(coin)
        price = self.prices.get(coin)
        if not pos or price is None:
            return
        fraction = min(max(fraction, 0.0), 1.0)
        if fraction < 1 and abs(pos["qty"]) * (1 - fraction) * price < self.s.strategy.min_trade_usd:
            fraction = 1.0  # don't leave a dust position behind
        if fraction <= 0:
            return
        long = pos["qty"] > 0
        qty = abs(pos["qty"]) * fraction
        basis = pos["cost_basis"] * fraction
        fill = (self.broker.sell(qty, price, reduce_only=True, coin=coin) if long
                else self.broker.buy_qty(qty, price, reduce_only=True, coin=coin))
        pnl = fill.cash_delta - basis
        self.store.set("cash", self.cash + fill.cash_delta)
        if fraction >= 1:
            self._save_position(coin, None)
        else:
            pos["qty"] = pos["qty"] - qty if long else pos["qty"] + qty
            pos["cost_basis"] -= basis
            self._save_position(coin, pos)
        self._sync_stop(coin, now)
        self.store.add_trade(now, fill, pnl, self.market.usd_to_aud(), reason, coin=coin)
        if hasattr(self.brain, "on_close"):
            self.brain.on_close(self, coin, pos, pnl, fraction, now, fill.price, reason)
        what = "SELL" if long else "BUY BACK"
        label = f"{what} {fraction:.0%} of {coin}:" if fraction < 1 else ("SELL" if long else "CLOSE SHORT")
        self.store.log(f"{label} {fill.qty:.6g} {coin} at {fmt_px(fill.price)}. "
                       f"{'Profit' if pnl >= 0 else 'Loss'} {pnl:+.2f} USDC. {reason}", level="trade", ts=now)

    def _close_all(self, now: int, reason: str) -> None:
        for coin in list(self.positions):
            self._close(coin, now, reason)

    # Controls from the dashboard
    def kill(self) -> None:
        with self.lock:
            if self.status == DEAD:
                return
            now = int(self.clock())
            self.prices = self._fetch_prices()
            self._close_all(now, "Kill switch pressed.")
            self.store.set("status", STOPPED)
            self.store.log("Kill switch: everything closed, trading stopped until you press Resume.", level="warning", ts=now)

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
        equity = self.equity() if self.prices else None
        start = self.contributed
        hosting = -self.store.ledger_total("running_cost")
        ai = -self.store.ledger_total("ai_cost")
        funding = -self.store.ledger_total("funding")
        realised = sum(t["pnl"] or 0 for t in self.store.trades(100000))
        burn = self.daily_burn()
        runway = (equity - self.floor) / burn if equity is not None and burn > 0 else None
        start_price = self.store.get("start_price")
        positions = []
        unrealised_total = 0.0
        for coin, p in self.positions.items():
            px = self.prices.get(coin)
            unreal = p["qty"] * px - p["cost_basis"] if px else None
            unrealised_total += unreal or 0
            positions.append({"coin": coin, "side": "long" if p["qty"] > 0 else "short", "qty": p["qty"],
                              "entry": abs(p["cost_basis"] / p["qty"]) if p["qty"] else None, "price": px,
                              "cost_basis": p["cost_basis"], "stop": p.get("stop"), "setup": p.get("setup"),
                              "value": abs(p["qty"]) * px if px else None, "unrealised": unreal, "target": p.get("target"),
                              "r_now": (px - p["entry_px"]) / p["risk_unit"] * (1 if p["qty"] > 0 else -1)
                              if px and p.get("risk_unit") and p.get("entry_px") else None,
                              "stage": "trailing" if p.get("half_taken") else "breakeven" if p.get("be_done") else None})
        return {
            "bot": self.brain.name,
            "label": self.brain.label,
            "mode": self.broker.venue.network if self.live else "paper",
            "status": self.status,
            "pair": self.s.bot.pair,
            "asset": self.primary,
            "interval_minutes": self.s.bot.interval_minutes,
            "price": price,
            "cash": self.cash,
            "qty": self.qty,
            "equity": equity,
            "starting_balance": start,
            "total_pnl": equity - start if equity is not None else None,
            "trading_pnl": realised + unrealised_total - funding,
            "costs_paid": hosting + ai,
            "hosting_paid": hosting,
            "ai_paid": ai,
            "funding_paid": funding,
            "monthly_cost": self.s.survival.monthly_running_cost_usd,
            "daily_burn": burn,
            "floor": self.floor,
            "bankroll_base": self.bankroll_base,
            "available_to_add": self.available_to_add(),
            "runway_days": max(runway, 0) if runway is not None else None,
            "buy_hold_pct": (price / start_price - 1) * 100 if price and start_price else None,
            "funding_rate": self.market.last_funding if hasattr(self.market, "last_funding") else None,
            "positions": positions,
            "exposure": self.exposure() if self.prices else 0.0,
            "max_positions": self.g.max_positions,
            "open_risk": self.open_risk() if self.prices else None,
            "btc_exposure": self.btc_exposure() if self.prices and self.g.max_btc_exposure else None,
            "max_btc_exposure": self.g.max_btc_exposure,
            "drawdown": self.store.get("drawdown"),
            "dd_pause_until": self.store.get("dd_pause_until"),
            "max_total_risk": self.g.max_total_risk,
            "min_volume_usd": self.g.min_volume_usd,
            "probation": bool(self.store.get("probation")),
            "started_at": self.store.get("started_at"),
            "day_start_equity": self.store.get("day_start_equity"),
            "daily_loss_limit_pct": self.g.daily_loss_limit_pct,
            "style": self.g.style,
            "last_error": self.last_error,
            **self.brain.extra_summary(self),
        }
