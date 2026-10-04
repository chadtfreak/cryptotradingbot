"""Backtests every playbook setup on years of history and writes the results as a table
Claude reads before each decision: real evidence of what has worked in these markets, and when.

Run with `python -m bot research` (history.py fetches the data). Uses closed 4h candles only
(no look-ahead), assumes the stop is hit first when a candle touches both stop and target,
and charges fees.

Each setup's rules are mechanical approximations of the playbook. Claude applies judgement
on top, so treat these as base rates, not promises."""

from datetime import datetime, timezone
from pathlib import Path

from .indicators import atr, ema, rsi

OUT = Path(__file__).parent / "knowledge" / "setup_stats.md"
REGIMES = Path(__file__).parent / "knowledge" / "setup_regimes.json"
DIMENSIONS = {"btc": ["BTC up", "BTC down"], "trend": ["trending", "choppy"],
              "funding": ["longs crowded", "normal", "shorts crowded"], "vol": ["high vol", "low vol"]}
FEE_ROUND_TRIP = 0.0009 + 0.0004  # taker fees both ways plus a little slippage
MAX_HOLD = 30  # 4h candles: 5 days
RANGE = 120  # 20 days of 4h candles


def signals(c, i, f, s, a, r, funding=None):
    """Setups firing on candle i: list of (setup, direction, stop_atr, target_atr).
    funding is the yearly funding rate in percent at that candle, when known."""
    out = []
    close, prev = c[i].close, c[i - 1].close
    hi = max(x.high for x in c[i - RANGE:i])
    lo = min(x.low for x in c[i - RANGE:i])
    prev_hi = max(x.high for x in c[i - RANGE - 1:i - 1])
    prev_lo = min(x.low for x in c[i - RANGE - 1:i - 1])
    up, down = f[i] > s[i], f[i] < s[i]
    gap = abs(f[i] / s[i] - 1) * 100
    if close > hi and up:
        out.append(("breakout", 1, 1.5, 3.0))
    if close < lo and down:
        out.append(("breakout", -1, 1.5, 3.0))
    if up and c[i].low <= f[i] < close and close > c[i].open and close < hi:
        out.append(("pullback", 1, 1.5, 3.0))
    if down and c[i].high >= f[i] > close and close < c[i].open and close > lo:
        out.append(("pullback", -1, 1.5, 3.0))
    if gap < 0.5 and r[i] < 30:
        out.append(("range_fade", 1, 1.5, 2.0))
    if gap < 0.5 and r[i] > 70:
        out.append(("range_fade", -1, 1.5, 2.0))
    if prev > prev_hi and close < prev_hi:
        out.append(("failed_breakout", -1, 1.0, 2.0))
    if prev < prev_lo and close > prev_lo:
        out.append(("failed_breakout", 1, 1.0, 2.0))
    ch24 = (close / c[i - 6].close - 1) * 100
    rng_hi = max(x.high for x in c[i - 5:i + 1])
    rng_lo = min(x.low for x in c[i - 5:i + 1])
    if ch24 > 8 and up and close >= rng_hi - 0.2 * (rng_hi - rng_lo) and r[i] < 80:
        out.append(("momentum", 1, 1.5, 3.0))
    if ch24 < -8 and down and close <= rng_lo + 0.2 * (rng_hi - rng_lo) and r[i] > 20:
        out.append(("momentum", -1, 1.5, 3.0))
    if funding is not None:  # crowded positioning: fade it once price is stretched the same way
        if funding > 40 and r[i] > 65 and close >= hi * 0.97:
            out.append(("squeeze_fade", -1, 1.5, 3.0))
        if funding < -15 and r[i] < 35 and close <= lo * 1.03:
            out.append(("squeeze_fade", 1, 1.5, 3.0))
    return out


def simulate(c, i, direction, stop_atr, target_atr, a) -> float:
    """Result of one trade in R (multiples of the amount risked), after fees."""
    entry = c[i].close
    risk = stop_atr * a[i]
    stop = entry - direction * risk
    target = entry + direction * target_atr * a[i]
    fee_r = FEE_ROUND_TRIP * entry / risk
    for j in range(i + 1, min(i + 1 + MAX_HOLD, len(c))):
        if (direction > 0 and c[j].low <= stop) or (direction < 0 and c[j].high >= stop):
            return -1.0 - fee_r
        if (direction > 0 and c[j].high >= target) or (direction < 0 and c[j].low <= target):
            return target_atr / stop_atr - fee_r
    last = c[min(i + MAX_HOLD, len(c) - 1)].close
    return direction * (last - entry) / risk - fee_r


def backtest_coin(candles, funding=None) -> list[tuple[str, int, float, int]]:
    """(setup, direction, R, entry index) for every trade, one open trade per setup at a time.
    funding, if given, is a function from a candle's time to the yearly funding rate."""
    closes = [x.close for x in candles]
    f, s = ema(closes, 20), ema(closes, 50)
    a = atr([x.high for x in candles], [x.low for x in candles], closes, 14)
    r = rsi(closes)
    busy_until: dict[str, int] = {}
    trades = []
    for i in range(RANGE + 2, len(candles) - 1):
        if None in (f[i], s[i], a[i], r[i]) or not a[i]:
            continue
        fr = funding(candles[i].ts) if funding else None
        for setup, d, sa, ta in signals(candles, i, f, s, a, r, fr):
            key = f"{setup}{d}"
            if busy_until.get(key, -1) >= i:
                continue
            res = simulate(candles, i, d, sa, ta, a)
            trades.append((setup, d, res, i))
            busy_until[key] = i + MAX_HOLD
    return trades


def stats(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0}
    wins = [x for x in rs if x > 0]
    return {"n": n, "win": len(wins) / n * 100, "exp": sum(rs) / n, "total": sum(rs),
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(x for x in rs if x <= 0) / max(n - len(wins), 1)}


def regime(btc_up: bool | None, trend_atr: float, funding: float | None, vol_pct: float, median_vol: float) -> dict:
    """The market mood a trade happens in. Used the same way on history and live."""
    return {
        "btc": None if btc_up is None else ("BTC up" if btc_up else "BTC down"),
        "trend": "trending" if trend_atr >= 1 else "choppy",
        "funding": None if funding is None else ("longs crowded" if funding > 30 else "shorts crowded" if funding < 0 else "normal"),
        "vol": "high vol" if vol_pct > median_vol else "low vol",
    }


def run_long(data: dict) -> str:
    """The same setups on years of Binance history, split by market mood, so Claude knows not
    just whether a setup works but when."""
    return report(collect(data), len(data))


def collect(data: dict) -> list:
    from .history import _funding_lookup, indicators

    btc = indicators(data["BTC"]["candles"])
    btc_pos = {t: k for k, t in enumerate(btc["ts"])}
    rows = []  # (setup, direction, R, regimes, ts)
    for coin, d in data.items():
        candles = d["candles"]
        fund = _funding_lookup(d["funding"]) if d["funding"] else None
        ind = indicators(candles)
        atr_pcts = sorted(a / c * 100 for a, c in zip(ind["atr"], ind["close"]) if a)
        median_vol = atr_pcts[len(atr_pcts) // 2] if atr_pcts else 0
        for setup, direction, res, i in backtest_coin(candles, fund):
            a, c = ind["atr"][i], ind["close"][i]
            j = btc_pos.get(candles[i].ts)
            fr = fund(candles[i].ts) if fund else None
            trend_atr = abs(ind["ema20"][i] / ind["ema50"][i] - 1) * 100 / (a / c * 100)
            btc_up = btc["ema20"][j] > btc["ema50"][j] if j is not None and btc["ema50"][j] else None
            rows.append((setup, direction, res, regime(btc_up, trend_atr, fr, a / c * 100, median_vol), candles[i].ts))
    return rows


def cells(rows: list) -> dict:
    """Expectancy by setup, side and market mood, for matching live conditions: {"breakout|1|btc|BTC up": [trades, R]}."""
    out = {}
    for setup in {r[0] for r in rows}:
        for d in (1, -1):
            mine = [r for r in rows if r[0] == setup and r[1] == d]
            if not mine:
                continue
            t = stats([r[2] for r in mine])
            out[f"{setup}|{d}"] = [t["n"], round(t["exp"], 3)]
            for key, values in DIMENSIONS.items():
                for v in values:
                    t = stats([r[2] for r in mine if r[3][key] == v])
                    if t["n"]:
                        out[f"{setup}|{d}|{key}|{v}"] = [t["n"], round(t["exp"], 3)]
    return out


def report(rows: list, n_coins: int) -> str:

    first = datetime.fromtimestamp(min(r[4] for r in rows), timezone.utc).strftime("%b %Y")
    last = datetime.fromtimestamp(max(r[4] for r in rows), timezone.utc).strftime("%b %Y")
    year_ago = max(r[4] for r in rows) - 365 * 86400
    fmt = lambda x: f"{x['exp']:+.2f}R ({x['n']})" if x["n"] >= 15 else (f"{x['exp']:+.2f}R ({x['n']}, too few)" if x["n"] else "none")
    by = lambda pred: [r[2] for r in rows if pred(r)]
    setups = sorted({r[0] for r in rows}, key=lambda k: -stats(by(lambda r: r[0] == k))["exp"])
    lines = [f"Backtest of the playbook setups on {n_coins} major coins, Binance 4h candles, {first} to {last}, through bull, bear and choppy markets. "
             "R = multiples of the amount risked, after fees, 1.5 ATR stop and 2 to 3 ATR target. Expectancy is the average R per trade: "
             "above about +0.1 is a real edge, below 0 loses money. Numbers in brackets are trade counts. These are mechanical versions "
             "of each setup and base rates, not promises; where a setup only works in one kind of market, trade it only there. "
             "Splits with fewer than about 300 trades are noisy, so don't lean hard on a single small cell. "
             "Where these numbers disagree with the playbook, trust the numbers.",
             "", "setup | trades | win rate | expectancy | long | short | last 12 months"]
    for k in setups:
        t = stats(by(lambda r: r[0] == k))
        lines.append(f"{k} | {t['n']} | {t['win']:.0f}% | {t['exp']:+.2f}R | {fmt(stats(by(lambda r: r[0] == k and r[1] > 0)))} | "
                     f"{fmt(stats(by(lambda r: r[0] == k and r[1] < 0)))} | {fmt(stats(by(lambda r: r[0] == k and r[4] >= year_ago)))}")
    dims = [("btc", ["BTC up", "BTC down"], "When BTC's 4h trend is up or down"),
            ("trend", ["trending", "choppy"], "When the coin itself is trending (20 EMA at least 1 ATR from the 50) or choppy"),
            ("funding", ["longs crowded", "normal", "shorts crowded"], "By funding (longs crowded = over 30% a year, shorts crowded = negative)"),
            ("vol", ["high vol", "low vol"], "When the coin is more or less volatile than usual")]
    for key, values, title in dims:
        lines += ["", f"{title}: setup and side | " + " | ".join(values)]
        for k in setups:
            for d, side in ((1, "long"), (-1, "short")):
                cells = [stats(by(lambda r: r[0] == k and r[1] == d and r[3][key] == v)) for v in values]
                if sum(c["n"] for c in cells) >= 30:
                    lines.append(f"{k} {side} | " + " | ".join(fmt(c) for c in cells))
    lines += ["", "squeeze_fade here means: funding over 40% a year with RSI over 65 near the 20-day high (short), or funding below -15% "
              "with RSI under 35 near the 20-day low (long). Binance funding is used as the measure of crowding."]
    return "\n".join(lines)
