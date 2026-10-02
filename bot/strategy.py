"""Trend following on EMA crossovers with a trailing ATR stop.

The rules, in plain English:
  * Buy when the fast EMA crosses above the slow EMA on a closed candle.
  * Place a stop a few ATRs below price. Each new candle, drag it up behind
    price but never move it down.
  * Sell when the stop is hit, or when the fast EMA crosses back below the slow.
  * Size every trade so that hitting the stop costs about 2% of equity.

Spot only, no leverage, one position at a time. Pure functions so the live
engine and the backtester run exactly the same logic.
"""

from dataclasses import dataclass

from .config import StrategySettings
from .indicators import atr, ema
from .market import Candle


@dataclass
class Decision:
    action: str  # "buy" (go long), "short", "sell" (close) or "hold"
    reason: str
    stop: float | None = None
    size_usd: float | None = None  # None means size by the risk rule
    sell_fraction: float = 1.0  # for sells: share of the position to sell


@dataclass
class Snapshot:
    close: float
    fast: float
    slow: float
    prev_fast: float
    prev_slow: float
    atr: float


def snapshot(candles: list[Candle], s: StrategySettings) -> Snapshot | None:
    closes = [c.close for c in candles]
    fast = ema(closes, s.fast_ema)
    slow = ema(closes, s.slow_ema)
    vol = atr([c.high for c in candles], [c.low for c in candles], closes, s.atr_period)
    if len(candles) < 2 or None in (fast[-2], slow[-2], vol[-1]):
        return None
    return Snapshot(closes[-1], fast[-1], slow[-1], fast[-2], slow[-2], vol[-1])


def on_candle_close(candles: list[Candle], s: StrategySettings, in_position: bool, stop: float | None) -> Decision:
    snap = snapshot(candles, s)
    if snap is None:
        return Decision("hold", "Warming up: not enough price history yet.")

    if not in_position:
        crossed_up = snap.prev_fast <= snap.prev_slow and snap.fast > snap.slow
        if crossed_up:
            new_stop = snap.close - s.atr_stop_mult * snap.atr
            return Decision(
                "buy",
                f"Uptrend started: {s.fast_ema} EMA ({snap.fast:,.2f}) crossed above "
                f"{s.slow_ema} EMA ({snap.slow:,.2f}). Stop set at {new_stop:,.2f}.",
                new_stop,
            )
        trend = "up" if snap.fast > snap.slow else "down"
        return Decision("hold", f"No new crossover. Trend is {trend}, waiting for a fresh entry.")

    if snap.fast < snap.slow:
        return Decision(
            "sell",
            f"Trend flipped: {s.fast_ema} EMA ({snap.fast:,.2f}) dropped below "
            f"{s.slow_ema} EMA ({snap.slow:,.2f}).",
        )
    trailed = snap.close - s.atr_stop_mult * snap.atr
    new_stop = max(stop or trailed, trailed)
    if stop is not None and new_stop > stop:
        return Decision("hold", f"Still trending up. Stop raised from {stop:,.2f} to {new_stop:,.2f}.", new_stop)
    return Decision("hold", f"Still trending up. Stop holds at {new_stop:,.2f}.", new_stop)


def stop_hit(price: float, stop: float | None, qty: float = 1.0) -> bool:
    if stop is None or qty == 0:
        return False
    return price <= stop if qty > 0 else price >= stop


def position_size(equity: float, cash: float, price: float, stop: float, s: StrategySettings, gas: float) -> float:
    """USD notional to buy. Returns 0 if the trade is too small to bother with."""
    distance = price - stop
    if distance <= 0:
        return 0.0
    by_risk = equity * s.risk_per_trade / (distance / price)
    by_cash = cash * s.max_position_pct - gas
    notional = min(by_risk, by_cash)
    return notional if notional >= s.min_trade_usd else 0.0
