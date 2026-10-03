"""Price data from Kraken's public API (no account or key needed)."""

from dataclasses import dataclass

import httpx

KRAKEN = "https://api.kraken.com/0/public"


@dataclass(frozen=True)
class Candle:
    ts: int  # open time, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


class KrakenMarket:
    def __init__(self, pair: str, timeout: float = 15.0):
        self.pair = pair
        self.client = httpx.Client(timeout=timeout)

    def _get(self, endpoint: str, params: dict) -> dict:
        resp = self.client.get(f"{KRAKEN}/{endpoint}", params=params)
        resp.raise_for_status()
        body = resp.json()
        if body.get("error"):
            raise RuntimeError(f"Kraken error: {body['error']}")
        return body["result"]

    def candles(self, interval_minutes: int, pair: str | None = None) -> list[Candle]:
        """Closed candles only, oldest first (Kraken returns up to 720)."""
        result = self._get("OHLC", {"pair": pair or self.pair, "interval": interval_minutes})
        rows = next(v for k, v in result.items() if k != "last")
        candles = [
            Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6]))
            for r in rows
        ]
        # The final row is the candle still forming, so drop it.
        return candles[:-1]

    def price(self) -> float:
        result = self._get("Ticker", {"pair": self.pair})
        return float(next(iter(result.values()))["c"][0])

    def usd_to_aud(self) -> float | None:
        """USDT/AUD rate, recorded against each trade for the ATO."""
        try:
            result = self._get("Ticker", {"pair": "USDTAUD"})
            return float(next(iter(result.values()))["c"][0])
        except Exception:
            return None

    last_funding: float | None = None

    def perp_context(self, coin: str = "ETH") -> dict | None:
        """Hyperliquid perp stats: hourly funding rate, open interest and mark price."""
        try:
            resp = self.client.post("https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"})
            resp.raise_for_status()
            meta, ctxs = resp.json()
            i = [u["name"] for u in meta["universe"]].index(coin)
            c = ctxs[i]
            out = {"funding": float(c["funding"]), "open_interest": float(c["openInterest"]),
                   "mark": float(c["markPx"]), "volume_24h": float(c.get("dayNtlVlm") or 0)}
            self.last_funding = out["funding"]
            return out
        except Exception:
            return None

    def funding_rate(self, coin: str = "ETH") -> float | None:
        ctx = self.perp_context(coin)
        return ctx["funding"] if ctx else None

    def fear_greed(self) -> tuple[int, str] | None:
        """Crypto Fear & Greed Index (0 = extreme fear, 100 = extreme greed)."""
        try:
            resp = self.client.get("https://api.alternative.me/fng/", params={"limit": 1})
            resp.raise_for_status()
            d = resp.json()["data"][0]
            return int(d["value"]), d["value_classification"]
        except Exception:
            return None


HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
INTERVALS = {1: "1m", 5: "5m", 15: "15m", 60: "1h", 240: "4h", 1440: "1d"}


class HyperliquidMarket:
    """Prices, candles, funding and volume for every Hyperliquid perp (public, no account needed).

    Always reads the real (mainnet) market, which is what analysis should be based on,
    even when orders go to the testnet."""

    def __init__(self, primary: str = "ETH", timeout: float = 15.0, url: str = HYPERLIQUID_INFO):
        self.primary = primary
        self.url = url
        self.client = httpx.Client(timeout=timeout)
        self.kraken = KrakenMarket("ETHUSDT", timeout)
        self._universe: tuple[float, list[dict]] | None = None
        self.last_funding: float | None = None

    def _post(self, body: dict):
        resp = self.client.post(self.url, json=body)
        resp.raise_for_status()
        return resp.json()

    def mids(self) -> dict[str, float]:
        return {k: float(v) for k, v in self._post({"type": "allMids"}).items() if not k.startswith("@")}

    def price(self, coin: str | None = None) -> float:
        return self.mids()[coin or self.primary]

    def candles(self, interval_minutes: int, coin: str | None = None, count: int = 300, pair: str | None = None) -> list[Candle]:
        """Closed candles only, oldest first."""
        import time as _t
        if pair == "XBTUSDT":
            coin = "BTC"
        now = int(_t.time() * 1000)
        start = now - (count + 1) * interval_minutes * 60_000
        rows = self._post({"type": "candleSnapshot", "req": {"coin": coin or self.primary,
                           "interval": INTERVALS[interval_minutes], "startTime": start, "endTime": now}})
        candles = [Candle(int(r["t"] // 1000), float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), float(r["v"]))
                   for r in rows]
        return [c for c in candles if (c.ts + interval_minutes * 60) * 1000 <= now]  # drop the candle still forming

    def universe(self, max_age: float = 60.0) -> list[dict]:
        """Every listed perp with its 24h volume, funding, open interest and prices. Cached briefly."""
        import time as _t
        if self._universe and _t.time() - self._universe[0] < max_age:
            return self._universe[1]
        meta, ctxs = self._post({"type": "metaAndAssetCtxs"})
        rows = []
        for u, c in zip(meta["universe"], ctxs):
            if u.get("isDelisted") or not c.get("markPx"):
                continue
            rows.append({"coin": u["name"], "volume": float(c.get("dayNtlVlm") or 0), "funding": float(c["funding"]),
                         "open_interest": float(c.get("openInterest") or 0), "mark": float(c["markPx"]),
                         "prev_day": float(c.get("prevDayPx") or c["markPx"]), "max_leverage": u.get("maxLeverage"),
                         "sz_decimals": u["szDecimals"]})
        rows.sort(key=lambda r: -r["volume"])
        self._universe = (_t.time(), rows)
        return rows

    def liquid(self, min_volume: float) -> list[str]:
        return [r["coin"] for r in self.universe() if r["volume"] >= min_volume]

    def perp_context(self, coin: str | None = None) -> dict | None:
        try:
            row = next(r for r in self.universe() if r["coin"] == (coin or self.primary))
        except Exception:
            return None
        out = {"funding": row["funding"], "open_interest": row["open_interest"], "mark": row["mark"], "volume_24h": row["volume"]}
        if (coin or self.primary) == self.primary:
            self.last_funding = out["funding"]
        return out

    def funding_rate(self, coin: str | None = None) -> float | None:
        ctx = self.perp_context(coin)
        return ctx["funding"] if ctx else None

    def usd_to_aud(self) -> float | None:
        return self.kraken.usd_to_aud()

    def fear_greed(self):
        return self.kraken.fear_greed()
