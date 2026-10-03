"""Backtests every playbook setup on every liquid coin and writes the results as a table
Claude reads before each decision: real evidence of what has worked in these markets.

Run with `python -m bot research`. Uses closed 4h candles only (no look-ahead), assumes
the stop is hit first when a candle touches both stop and target, and charges fees.

Each setup's rules are mechanical approximations of the playbook. Claude applies judgement
on top, so treat these as base rates, not promises."""

import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .indicators import atr, ema, rsi

OUT = Path(__file__).parent / "knowledge" / "setup_stats.md"
FEE_ROUND_TRIP = 0.0009 + 0.0004  # taker fees both ways plus a little slippage
MAX_HOLD = 30  # 4h candles: 5 days
RANGE = 120  # 20 days of 4h candles


def signals(c, i, f, s, a, r):
    """Setups firing on candle i: list of (setup, direction, stop_atr, target_atr)."""
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


def backtest_coin(candles) -> list[tuple[str, int, float, int]]:
    """(setup, direction, R, entry index) for every trade, one open trade per setup at a time."""
    closes = [x.close for x in candles]
    f, s = ema(closes, 20), ema(closes, 50)
    a = atr([x.high for x in candles], [x.low for x in candles], closes, 14)
    r = rsi(closes)
    busy_until: dict[str, int] = {}
    trades = []
    for i in range(RANGE + 2, len(candles) - 1):
        if None in (f[i], s[i], a[i], r[i]) or not a[i]:
            continue
        for setup, d, sa, ta in signals(candles, i, f, s, a, r):
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


def run(market, min_volume: float = 50e6, extra: int = 10, log=print) -> str:
    coins = market.liquid(min_volume)
    for r in market.universe():  # widen the sample a little with the next most traded coins
        if len(coins) >= len(market.liquid(min_volume)) + extra:
            break
        if r["coin"] not in coins:
            coins.append(r["coin"])
    by_setup: dict[str, list[float]] = defaultdict(list)
    by_dir: dict[tuple, list[float]] = defaultdict(list)
    by_coin: dict[tuple, list[float]] = defaultdict(list)
    recent: dict[str, list[float]] = defaultdict(list)
    spans = []
    for coin in coins:
        try:
            candles = market.candles(240, coin=coin, count=5000)
        except Exception as exc:
            log(f"  {coin}: skipped ({exc})")
            continue
        if len(candles) < RANGE + 100:
            continue
        spans.append((candles[0].ts, candles[-1].ts))
        cutoff = len(candles) - 6 * 90  # last 90 days
        for setup, d, res, i in backtest_coin(candles):
            by_setup[setup].append(res)
            by_dir[(setup, d)].append(res)
            by_coin[(setup, coin)].append(res)
            if i >= cutoff:
                recent[setup].append(res)
        log(f"  {coin}: {len(candles)} candles")
        time.sleep(0.2)

    start = datetime.fromtimestamp(min(s for s, _ in spans), timezone.utc).strftime("%b %Y")
    end = datetime.fromtimestamp(max(e for _, e in spans), timezone.utc).strftime("%d %b %Y")
    lines = [f"Backtest of the playbook setups on {len(spans)} Hyperliquid coins, 4h candles, {start} to {end}. "
             f"R = multiples of the amount risked, after fees. Expectancy is the average R per trade: above about +0.1 is a real edge, "
             f"below 0 loses money. These are mechanical versions of each setup; your judgement should beat them, but don't ignore them.",
             "", "setup | trades | win rate | avg win | avg loss | expectancy | last 90 days expectancy (trades)"]
    order = sorted(by_setup, key=lambda k: -stats(by_setup[k])["exp"])
    for k in order:
        t, rc = stats(by_setup[k]), stats(recent.get(k, []))
        rec = f"{rc['exp']:+.2f}R ({rc['n']})" if rc["n"] else "none"
        lines.append(f"{k} | {t['n']} | {t['win']:.0f}% | {t['avg_win']:+.2f}R | {t['avg_loss']:+.2f}R | {t['exp']:+.2f}R | {rec}")
    lines += ["", "By direction: setup | long expectancy (trades) | short expectancy (trades)"]
    for k in order:
        lo, sh = stats(by_dir[(k, 1)]), stats(by_dir[(k, -1)])
        fmt = lambda x: f"{x['exp']:+.2f}R ({x['n']})" if x["n"] else "none"
        lines.append(f"{k} | {fmt(lo)} | {fmt(sh)}")
    lines += ["", "Best and worst coins per setup (at least 8 trades): setup | best | worst"]
    for k in order:
        rows = [(coin, stats(v)) for (sk, coin), v in by_coin.items() if sk == k and len(v) >= 8]
        rows.sort(key=lambda x: -x[1]["exp"])
        best = ", ".join(f"{c} {s['exp']:+.2f}R" for c, s in rows[:3]) or "n/a"
        worst = ", ".join(f"{c} {s['exp']:+.2f}R" for c, s in rows[-3:][::-1]) or "n/a"
        lines.append(f"{k} | {best} | {worst}")
    lines += ["", "Not backtested: squeeze_fade (needs funding history). Treat it as unproven and size it small."]
    return "\n".join(lines)


def main() -> None:
    from .market import HyperliquidMarket
    print("Backtesting setups on Hyperliquid history...")
    text = run(HyperliquidMarket())
    OUT.write_text(text + "\n")
    print("\n" + text + f"\n\nSaved to {OUT}")
