"""Years of market history for research.

`python -m bot research` downloads 4h candles and funding rates for about 30 major coins from
Binance's free public archive (back to 2020) and backtests the playbook setups on them, split by
market mood, so Claude knows not just whether a setup works but when. Downloads are cached, so
later runs only fetch the new months.

(A "similar moments" lookup was tried on this data too: given only history before 2025, it
called 2025 to 2026 moves right 50% of the time, no better than a coin flip, so it isn't used.)
"""

import bisect
import csv
import io
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .indicators import atr, ema, rsi
from .market import Candle

ARCHIVE = "https://data.binance.vision/data/futures/um/monthly"

# Hyperliquid name: Binance USDT perpetual
COINS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "BNB": "BNBUSDT",
    "ADA": "ADAUSDT", "AVAX": "AVAXUSDT", "LINK": "LINKUSDT", "LTC": "LTCUSDT", "DOT": "DOTUSDT", "ATOM": "ATOMUSDT",
    "NEAR": "NEARUSDT", "SUI": "SUIUSDT", "APT": "APTUSDT", "ARB": "ARBUSDT", "OP": "OPUSDT", "INJ": "INJUSDT",
    "TIA": "TIAUSDT", "WLD": "WLDUSDT", "AAVE": "AAVEUSDT", "UNI": "UNIUSDT", "FIL": "FILUSDT", "BCH": "BCHUSDT",
    "TRX": "TRXUSDT", "ENA": "ENAUSDT", "HYPE": "HYPEUSDT", "kPEPE": "1000PEPEUSDT", "SEI": "SEIUSDT", "ETC": "ETCUSDT",
}
START = (2020, 1)

RANGE = 120  # 20 days of 4h candles
MIN_CANDLES = 300


# Downloading
def _months(start=START, end=None):
    end = end or (datetime.now(timezone.utc).year, datetime.now(timezone.utc).month)
    y, m = start
    while (y, m) < end:  # whole months only; the current month isn't in the archive yet
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _fetch(client, url: str, path: Path) -> bytes | None:
    if path.exists():
        data = path.read_bytes()
        return data or None
    for attempt in range(3):
        try:
            r = client.get(url)
            if r.status_code == 404:
                path.write_bytes(b"")
                return None
            r.raise_for_status()
            path.write_bytes(r.content)
            return r.content
        except httpx.HTTPError:
            time.sleep(1 + attempt)
    return None


def _csv_rows(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        text = z.read(z.namelist()[0]).decode()
    for row in csv.reader(io.StringIO(text)):
        if row and row[0][:1].isdigit():
            yield row


def _ms(v: str) -> int:
    t = int(float(v))
    return t // 1000 if t > 10**14 else t  # some newer files use microseconds


def download(cache: Path, coins=None, log=print) -> dict:
    """{coin: {"candles": [...], "funding": [(ts, apr), ...]}} from the archive, cached on disk."""
    coins = coins or list(COINS)
    cache.mkdir(parents=True, exist_ok=True)
    jobs = []
    for coin in coins:
        sym = COINS[coin]
        (cache / sym).mkdir(exist_ok=True)
        for y, m in _months():
            ym = f"{y}-{m:02d}"
            jobs.append((coin, "k", f"{ARCHIVE}/klines/{sym}/4h/{sym}-4h-{ym}.zip", cache / sym / f"k-{ym}.zip"))
            jobs.append((coin, "f", f"{ARCHIVE}/fundingRate/{sym}/{sym}-fundingRate-{ym}.zip", cache / sym / f"f-{ym}.zip"))
    with httpx.Client(timeout=30) as client, ThreadPoolExecutor(16) as pool:
        blobs = list(pool.map(lambda j: _fetch(client, j[2], j[3]), jobs))
    out = {c: {"candles": [], "funding": []} for c in coins}
    for (coin, kind, _, _), blob in zip(jobs, blobs):
        if not blob:
            continue
        for row in _csv_rows(blob):
            if kind == "k":
                out[coin]["candles"].append(Candle(_ms(row[0]) // 1000, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))
            else:
                hours = float(row[1]) if len(row) > 2 else 8.0
                rate = float(row[-1])
                out[coin]["funding"].append((_ms(row[0]) // 1000, rate * (24 / hours) * 365 * 100))
    for coin, d in out.items():
        d["candles"] = sorted({c.ts: c for c in d["candles"]}.values(), key=lambda c: c.ts)
        d["funding"].sort()
        log(f"  {coin}: {len(d['candles'])} candles, {len(d['funding'])} funding rates")
    return {c: d for c, d in out.items() if len(d["candles"]) > MIN_CANDLES}


# Indicators for research
def indicators(candles) -> dict:
    closes = [c.close for c in candles]
    highs, lows = [c.high for c in candles], [c.low for c in candles]
    hi, lo = [None] * len(candles), [None] * len(candles)
    for i in range(RANGE, len(candles)):
        hi[i] = max(highs[i - RANGE:i])
        lo[i] = min(lows[i - RANGE:i])
    return {"close": closes, "ts": [c.ts for c in candles], "ema20": ema(closes, 20), "ema50": ema(closes, 50),
            "atr": atr(highs, lows, closes, 14), "rsi": rsi(closes), "hi": hi, "lo": lo}


def _funding_lookup(funding):
    """Funding in force at a time: the latest rate published at or before it."""
    times = [t for t, _ in funding]

    def at(ts):
        k = bisect.bisect_right(times, ts) - 1
        return funding[k][1] if k >= 0 else None
    return at


def main(cache: Path) -> None:
    from . import research

    print("Downloading history from Binance's public archive (cached, so later runs are quick)...")
    data = download(cache)
    print("Backtesting the setups on the long history, by market mood...")
    text = research.run_long(data)
    research.OUT.write_text(text + "\n")
    print(text + f"\n\nSaved to {research.OUT}")
