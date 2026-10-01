"""Replays the strategy over historical candles to see whether the bot would have survived.

Uses the same strategy functions and cost model as the live engine. Stops are
checked against each candle's low and filled at the stop (or the open, if price
gapped through it). The daily loss limit isn't modelled.
"""

from dataclasses import dataclass, field

from .broker import PaperBroker
from .config import Settings
from .market import Candle
from .strategy import on_candle_close, position_size


@dataclass
class BacktestResult:
    start_ts: int
    end_ts: int
    start_equity: float
    end_equity: float
    buy_and_hold_pct: float
    trades: int
    wins: int
    fees_paid: float
    costs_paid: float
    max_drawdown_pct: float
    died: bool
    curve: list[tuple[int, float]] = field(default_factory=list)

    @property
    def return_pct(self) -> float:
        return (self.end_equity / self.start_equity - 1) * 100

    def report(self) -> str:
        days = (self.end_ts - self.start_ts) / 86400
        win_rate = f"{self.wins / self.trades * 100:.0f}%" if self.trades else "n/a"
        lines = [
            f"Period:            {days:.0f} days",
            f"Start equity:      {self.start_equity:.2f} USDT",
            f"End equity:        {self.end_equity:.2f} USDT ({self.return_pct:+.1f}%)",
            f"Buy and hold:      {self.buy_and_hold_pct:+.1f}%",
            f"Round trips:       {self.trades} (win rate {win_rate})",
            f"Trading fees+gas:  {self.fees_paid:.2f} USDT",
            f"Running costs:     {self.costs_paid:.2f} USDT",
            f"Max drawdown:      {self.max_drawdown_pct:.1f}%",
            f"Survived:          {'NO, hit the floor' if self.died else 'yes'}",
        ]
        return "\n".join(lines)


def run_backtest(candles: list[Candle], s: Settings, warmup: int | None = None) -> BacktestResult:
    broker = PaperBroker(s.costs)
    warmup = warmup or max(s.strategy.slow_ema, s.strategy.atr_period) + 1
    if len(candles) <= warmup + 1:
        raise ValueError(f"Need more than {warmup + 1} candles to backtest, got {len(candles)}.")
    cash, qty, cost_basis, stop = s.bot.starting_balance, 0.0, 0.0, None
    trades = wins = 0
    fees = costs = 0.0
    peak, max_dd = cash, 0.0
    died = False
    curve = []
    cost_per_candle = s.survival.daily_cost_usd * s.bot.interval_minutes / 1440

    def sell(price: float):
        nonlocal cash, qty, cost_basis, stop, trades, wins, fees
        fill = broker.sell(qty, price)
        pnl = fill.cash_delta - cost_basis
        cash += fill.cash_delta
        fees += fill.fee + fill.gas
        trades += 1
        wins += pnl > 0
        qty, cost_basis, stop = 0.0, 0.0, None

    for i in range(warmup, len(candles)):
        c = candles[i]
        cash -= cost_per_candle
        costs += cost_per_candle

        if qty > 0 and stop is not None and c.low <= stop:
            sell(min(stop, c.open))

        equity = cash + qty * c.close
        if equity < s.survival.floor_usd:
            if qty > 0:
                sell(c.close)
            died = True
            curve.append((c.ts, cash))
            break

        decision = on_candle_close(candles[: i + 1], s.strategy, qty > 0, stop)
        if decision.action == "buy":
            notional = position_size(equity, cash, c.close, decision.stop, s.strategy, s.costs.gas_per_swap_usd)
            if notional:
                fill = broker.buy(notional, c.close)
                cash += fill.cash_delta
                qty = fill.qty
                cost_basis = -fill.cash_delta
                fees += fill.fee + fill.gas
                stop = decision.stop
        elif decision.action == "sell":
            sell(c.close)
        elif decision.stop is not None:
            stop = decision.stop

        equity = cash + qty * c.close
        peak = max(peak, equity)
        max_dd = max(max_dd, (1 - equity / peak) * 100)
        curve.append((c.ts, equity))

    last = candles[min(i, len(candles) - 1)]
    end_equity = cash + qty * last.close
    first = candles[warmup]
    return BacktestResult(
        start_ts=first.ts, end_ts=last.ts, start_equity=s.bot.starting_balance, end_equity=end_equity,
        buy_and_hold_pct=(last.close / first.close - 1) * 100, trades=trades, wins=wins,
        fees_paid=fees, costs_paid=costs, max_drawdown_pct=max_dd, died=died, curve=curve,
    )
