"""Live trading on Hyperliquid's ETH perpetual, through a trade-only API wallet.

The API wallet signs orders for your main account but can never withdraw, so the
key that lives on the Umbrel can't be used to take the money. Every position gets a
real stop order on the exchange, which fires even if the bot is offline.
"""

from dataclasses import dataclass

from .broker import Fill
from .config import CostSettings

URLS = {
    "testnet": "https://api.hyperliquid-testnet.xyz",
    "mainnet": "https://api.hyperliquid.xyz",
}
MARKET_SLIPPAGE = 0.01  # market orders are IOC limits up to 1% through the mid
STOP_SLIPPAGE = 0.10  # once a stop triggers, accept up to 10% slippage to be sure it fills
MAX_SPREAD_PCT = 1.5  # don't open into a book with a wider gap than this between buyers and sellers
MAX_OFF_MARKET_PCT = 2.0  # or one priced this far from the real market (testnet books can drift)
MIN_ORDER_USD = 10.0  # Hyperliquid's minimum order value


class VenueError(RuntimeError):
    pass


@dataclass
class AccountState:
    account_value: float  # USDC, including unrealised profit or loss
    positions: dict  # coin -> (signed qty, entry price)
    primary: str = "ETH"

    @property
    def qty(self) -> float:
        return self.positions.get(self.primary, (0.0, None))[0]

    @property
    def entry_price(self) -> float | None:
        return self.positions.get(self.primary, (0.0, None))[1]


def round_price(px: float, sz_decimals: int) -> float:
    """Perp prices: at most 5 significant figures and 6 - szDecimals decimals."""
    return round(float(f"{px:.5g}"), 6 - sz_decimals)


def round_size(sz: float, sz_decimals: int) -> float:
    return round(sz, sz_decimals)


UNIFIED_MODES = ("unifiedAccount", "portfolioMargin")


def account_state(info, account: str, coin: str = "ETH") -> AccountState:
    """Account value and every open position, for either account type.

    Standard accounts keep a separate perps balance. Unified accounts (the default for
    new accounts) back perps with the spot USDC balance and report the perps value as
    zero, so there the value is spot USDC plus the open positions' unrealised profit.
    (Checked against a real unified testnet account: the spot total includes the margin
    held for positions but not their unrealised profit.)"""
    st = info.user_state(account)
    positions, upnl = {}, 0.0
    for p in st.get("assetPositions", []):
        pos = p["position"]
        if float(pos["szi"]) != 0:
            positions[pos["coin"]] = (float(pos["szi"]), float(pos["entryPx"]))
        upnl += float(pos.get("unrealizedPnl") or 0)
    mode = info.query_user_abstraction_state(account)
    if mode in UNIFIED_MODES:
        spot = sum(float(b["total"]) for b in info.spot_user_state(account).get("balances", []) if b["coin"] == "USDC")
        value = spot + upnl
    else:
        value = float(st["marginSummary"]["accountValue"])
    return AccountState(value, positions, coin)


def agent_address(agent_key: str) -> str:
    from eth_account import Account
    return Account.from_key(agent_key).address


class HyperliquidVenue:
    def __init__(self, network: str, account: str, agent_key: str, coin: str = "ETH", info=None, exchange=None):
        if network not in URLS:
            raise ValueError(f"Unknown network {network}")
        self.network = network
        self.account = account
        self.coin = coin  # the primary coin
        self._agent_key = agent_key
        self._info, self._exchange = info, exchange
        self._sz: dict[str, int] | None = None

    def _connect(self) -> None:
        """Connects on first use, so a network blip at startup doesn't stop the bot booting."""
        if self._info is None or self._exchange is None:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            self._info = self._info or Info(URLS[self.network], skip_ws=True)
            self._exchange = self._exchange or Exchange(Account.from_key(self._agent_key), URLS[self.network], account_address=self.account)

    @property
    def info(self):
        self._connect()
        return self._info

    @property
    def exchange(self):
        self._connect()
        return self._exchange

    def sz_decimals(self, coin: str | None = None) -> int:
        if self._sz is None:
            self._sz = {u["name"]: int(u["szDecimals"]) for u in self.info.meta()["universe"] if not u.get("isDelisted")}
        coin = coin or self.coin
        if coin not in self._sz:
            raise VenueError(f"{coin} isn't listed on Hyperliquid {self.network}")
        return self._sz[coin]

    # Reading
    def mids(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.info.all_mids().items() if not k.startswith("@")}

    def price(self, coin: str | None = None) -> float:
        return self.mids()[coin or self.coin]

    def state(self) -> AccountState:
        return account_state(self.info, self.account, self.coin)

    def book_problem(self, coin: str, real_price: float | None = None) -> str | None:
        """Why an order in this coin wouldn't fill sensibly right now, or None if the book looks fine.
        Testnet books for smaller coins are often thin, with big gaps or prices far from the real market."""
        try:
            self.sz_decimals(coin)
        except VenueError:
            return f"it isn't listed on Hyperliquid {self.network}"
        levels = (self.info.l2_snapshot(coin) or {}).get("levels") or [[], []]
        if not levels[0] or not levels[1]:
            return "its order book is empty"
        bid, ask = float(levels[0][0]["px"]), float(levels[1][0]["px"])
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 100
        if spread > MAX_SPREAD_PCT:
            return f"its order book is too thin ({spread:.1f}% gap between buyers and sellers)"
        if real_price and abs(mid / real_price - 1) * 100 > MAX_OFF_MARKET_PCT:
            return f"it's priced {(mid / real_price - 1) * 100:+.1f}% away from the real market here"
        return None

    def stop_orders(self, coin: str | None = None) -> list[dict]:
        orders = self.info.frontend_open_orders(self.account)
        return [o for o in orders if o["coin"] == (coin or self.coin) and o.get("isTrigger")]

    # Trading
    def _check(self, resp, what: str) -> dict:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise VenueError(f"{what} failed: {resp}")
        status = resp["response"]["data"]["statuses"][0]
        if "error" in status:
            raise VenueError(f"{what} rejected by Hyperliquid: {status['error']}")
        return status

    def market(self, is_buy: bool, qty: float, reduce_only: bool, coin: str | None = None) -> tuple[float, float]:
        """Market order. Returns (filled size, average price)."""
        coin = coin or self.coin
        dec = self.sz_decimals(coin)
        sz = round_size(qty, dec)
        if sz <= 0:
            raise VenueError("Order size rounds to zero")
        px = round_price(self.price(coin) * (1 + MARKET_SLIPPAGE if is_buy else 1 - MARKET_SLIPPAGE), dec)
        resp = self.exchange.order(coin, is_buy, sz, px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only)
        status = self._check(resp, f"{coin} market order")
        filled = status.get("filled")
        if not filled or float(filled["totalSz"]) == 0:
            raise VenueError(f"{coin} market order didn't fill: {status}")
        return float(filled["totalSz"]), float(filled["avgPx"])

    def set_leverage(self, leverage: int = 1, coin: str | None = None) -> None:
        """Sets the exchange's own leverage cap, so it refuses anything above it too."""
        self._check_action(self.exchange.update_leverage(leverage, coin or self.coin, True), "Setting leverage")

    def _check_action(self, resp, what: str) -> None:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise VenueError(f"{what} failed: {resp}")

    def cancel_stops(self, coin: str | None = None) -> None:
        for o in self.stop_orders(coin):
            self.exchange.cancel(coin or self.coin, o["oid"])

    def place_stop(self, qty: float, stop: float, coin: str | None = None) -> int:
        """Reduce-only stop market order for the whole position."""
        coin = coin or self.coin
        dec = self.sz_decimals(coin)
        is_buy = qty < 0  # a short's stop buys back
        sz = round_size(abs(qty), dec)
        trigger = round_price(stop, dec)
        limit = round_price(stop * (1 + STOP_SLIPPAGE if is_buy else 1 - STOP_SLIPPAGE), dec)
        resp = self.exchange.order(coin, is_buy, sz, limit,
                                   {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
        status = self._check(resp, f"{coin} stop order")
        return int((status.get("resting") or {}).get("oid") or 0)


class LiveBroker:
    """Same interface as PaperBroker, but fills are real Hyperliquid orders."""

    live = True

    def __init__(self, venue: HyperliquidVenue, costs: CostSettings):
        self.venue = venue
        self.costs = costs

    def _fill(self, side: str, qty: float, price: float) -> Fill:
        notional = qty * price
        return Fill(side, qty, price, notional, notional * self.costs.pool_fee_pct / 100, 0.0)

    def buy(self, notional: float, market_price: float, reduce_only: bool = False, coin: str | None = None) -> Fill:
        qty, px = self.venue.market(True, notional / market_price, reduce_only, coin=coin)
        return self._fill("buy", qty, px)

    def buy_qty(self, qty: float, market_price: float, reduce_only: bool = True, coin: str | None = None) -> Fill:
        qty, px = self.venue.market(True, qty, reduce_only, coin=coin)
        return self._fill("buy", qty, px)

    def sell(self, qty: float, market_price: float, reduce_only: bool = False, coin: str | None = None) -> Fill:
        qty, px = self.venue.market(False, qty, reduce_only, coin=coin)
        return self._fill("sell", qty, px)

    def sync_stop(self, qty: float, stop: float | None, coin: str | None = None) -> None:
        self.venue.cancel_stops(coin)
        if qty != 0 and stop is not None:
            self.venue.place_stop(qty, stop, coin=coin)


def check_credentials(network: str, account: str, agent_key: str) -> tuple[bool, str, float | None]:
    """Checks the account exists on that network and the API wallet is approved to trade for it."""
    try:
        if not (account.startswith("0x") and len(account) == 42):
            return False, "The account address should start with 0x and be 42 characters long.", None
        try:
            agent = agent_address(agent_key)
        except Exception:
            return False, "That API wallet private key isn't valid. It should start with 0x and be 66 characters long.", None
        from hyperliquid.info import Info
        info = Info(URLS[network], skip_ws=True)
        role = info.user_role(agent)
        owner = ((role or {}).get("data") or {}).get("user", "")
        if (role or {}).get("role") != "agent" or owner.lower() != account.lower():
            return False, (f"That API wallet ({agent}) isn't approved for {account} on {network}. "
                           "Create it under More, API on the Hyperliquid site while connected with your main wallet."), None
        value = account_state(info, account).account_value
        if value < MIN_ORDER_USD:
            spot = sum(float(b["total"]) for b in info.spot_user_state(account).get("balances", []) if b["coin"] == "USDC")
            if spot >= MIN_ORDER_USD:
                return False, (f"Your {spot:,.2f} USDC is in your Spot balance, but the bot trades perps. On the Hyperliquid "
                               "site, open Portfolio, tap Transfer and move it from Spot to Perps, then try again."), value
            return False, f"The account only has {value:.2f} USDC on {network}. It needs funds before the bot can trade.", value
        return True, "ok", value
    except Exception as exc:
        return False, f"Couldn't check with Hyperliquid ({type(exc).__name__}: {exc}).", None
