"""Turns trade decisions into fills.

PaperBroker simulates a Uniswap-style swap: price slippage, a pool fee and gas.
A live on-chain broker will implement the same two methods in stage 2.
"""

from dataclasses import dataclass

from .config import CostSettings


@dataclass
class Fill:
    side: str
    qty: float
    price: float  # effective price after slippage
    notional: float  # qty * price
    fee: float
    gas: float

    @property
    def cash_delta(self) -> float:
        """Change in USDT balance."""
        if self.side == "buy":
            return -(self.notional + self.fee + self.gas)
        return self.notional - self.fee - self.gas


class PaperBroker:
    def __init__(self, costs: CostSettings):
        self.costs = costs

    live = False

    def buy(self, notional: float, market_price: float, reduce_only: bool = False) -> Fill:
        price = market_price * (1 + self.costs.slippage_pct / 100)
        return Fill("buy", notional / price, price, notional, notional * self.costs.pool_fee_pct / 100, self.costs.gas_per_swap_usd)

    def buy_qty(self, qty: float, market_price: float, reduce_only: bool = True) -> Fill:
        """Buy an exact quantity, used to close a short."""
        price = market_price * (1 + self.costs.slippage_pct / 100)
        notional = qty * price
        return Fill("buy", qty, price, notional, notional * self.costs.pool_fee_pct / 100, self.costs.gas_per_swap_usd)

    def sell(self, qty: float, market_price: float, reduce_only: bool = False) -> Fill:
        price = market_price * (1 - self.costs.slippage_pct / 100)
        notional = qty * price
        return Fill("sell", qty, price, notional, notional * self.costs.pool_fee_pct / 100, self.costs.gas_per_swap_usd)

    def sync_stop(self, qty: float, stop: float | None) -> None:
        """Paper trading has no exchange; the engine checks stops itself."""
