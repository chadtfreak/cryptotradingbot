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

    def fear_greed(self) -> tuple[int, str] | None:
        """Crypto Fear & Greed Index (0 = extreme fear, 100 = extreme greed)."""
        try:
            resp = self.client.get("https://api.alternative.me/fng/", params={"limit": 1})
            resp.raise_for_status()
            d = resp.json()["data"][0]
            return int(d["value"]), d["value_classification"]
        except Exception:
            return None
