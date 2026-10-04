"""Free market scanner. Every hour, code looks across every liquid Hyperliquid coin and
ranks what's moving, so Claude only has to think about the interesting few."""

from .indicators import atr, ema, rsi

LOOKBACK_CANDLES = 130  # 4h candles: about 21 days
RANGE_CANDLES = 120  # 20 days, for breakouts


def coin_features(coin: str, candles, row: dict | None) -> dict | None:
    if len(candles) < 60:
        return None
    closes = [c.close for c in candles]
    last = candles[-1]
    prior = candles[-RANGE_CANDLES - 1:-1]
    hi, lo = max(c.high for c in prior), min(c.low for c in prior)
    fast, slow = ema(closes, 20), ema(closes, 50)
    f, s = fast[-1], slow[-1]
    atrs = atr([c.high for c in candles], [c.low for c in candles], closes, 14)
    vol = atrs[-1]
    funding = row["funding"] if row else 0.0
    vols = sorted(a / c * 100 for a, c in zip(atrs, closes) if a)
    feat = {
        "coin": coin,
        "price": last.close,
        "ch_4h": (last.close / last.open - 1) * 100,
        "ch_24h": (last.close / closes[-7] - 1) * 100,
        "ch_7d": (last.close / closes[-43] - 1) * 100,
        "trend": "up" if f > s else "down",
        "trend_gap": (f / s - 1) * 100,
        "rsi": rsi(closes)[-1],
        "atr_pct": vol / last.close * 100 if vol else 0.0,
        "atr": vol,
        "high_20d": hi,
        "low_20d": lo,
        "from_high": (last.close / hi - 1) * 100,
        "from_low": (last.close / lo - 1) * 100,
        "breakout": last.close > hi,
        "breakdown": last.close < lo,
        "funding_apr": funding * 24 * 365 * 100,
        "volume_m": (row or {}).get("volume", 0) / 1e6,
        "open_interest": (row or {}).get("open_interest"),
        "candle_ts": last.ts,
        "trend_atr": abs(f / s - 1) * 100 / (vol / last.close * 100) if vol else 0.0,
        "median_vol": vols[len(vols) // 2] if vols else 0.0,
        "setups": _setups(candles, fast, slow, atrs, closes, funding * 24 * 365 * 100),
    }
    # Rough "how interesting is this" score: big moves, breakouts, strong trends, crowded funding
    feat["score"] = (abs(feat["ch_24h"]) / max(feat["atr_pct"], 0.5)
                     + (3 if feat["breakout"] or feat["breakdown"] else 0)
                     + min(abs(feat["trend_gap"]), 5) / 2
                     + min(abs(feat["funding_apr"]) / 50, 3))
    return feat


def _setups(candles, fast, slow, atrs, closes, funding_apr) -> list[tuple[str, int]]:
    """Playbook setups firing on the latest closed candle, using the exact rules that were backtested."""
    from .research import RANGE, signals

    i = len(candles) - 1
    if i < RANGE + 2 or None in (fast[i], slow[i], atrs[i]):
        return []
    r = rsi(closes)
    if r[i] is None:
        return []
    return sorted({(k, d) for k, d, *_ in signals(candles, i, fast, slow, atrs, r, funding_apr)})


def alerts(feat: dict) -> list[tuple[str, str]]:
    """(kind, message) for things worth waking Claude for."""
    out = []
    c = feat["coin"]
    if feat["breakout"]:
        out.append(("breakout", f"{c} closed above its 20-day high ({feat['high_20d']:.6g})"))
    if feat["breakdown"]:
        out.append(("breakdown", f"{c} closed below its 20-day low ({feat['low_20d']:.6g})"))
    if abs(feat["ch_4h"]) >= 5:
        out.append(("move", f"{c} moved {feat['ch_4h']:+.1f}% in the last 4 hours"))
    if abs(feat["funding_apr"]) >= 100:
        out.append(("funding", f"{c} funding is extreme ({feat['funding_apr']:+.0f}% a year), one side is very crowded"))
    if feat.get("oi_24h") is not None and abs(feat["oi_24h"]) >= 25:
        out.append(("oi", f"{c} open interest changed {feat['oi_24h']:+.0f}% in 24 hours ({'new money piling in' if feat['oi_24h'] > 0 else 'positions being closed out'})"))
    return out


def scan(market, min_volume: float, held: list[str]) -> list[dict]:
    rows = {r["coin"]: r for r in market.universe()}
    coins = [c for c in market.liquid(min_volume)] if min_volume else []
    for c in held:
        if c not in coins:
            coins.append(c)
    out = []
    for coin in coins:
        try:
            feat = coin_features(coin, market.candles(240, coin=coin, count=LOOKBACK_CANDLES), rows.get(coin))
        except Exception:
            feat = None
        if feat:
            out.append(feat)
    out.sort(key=lambda f: -f["score"])
    return out


def table(features: list[dict], held: list[str], limit: int = 12) -> str:
    """Compact table for Claude: the top opportunities plus anything held."""
    pick = features[:limit] + [f for f in features[limit:] if f["coin"] in held]
    lines = ["coin | price | 4h | 24h | 7d | 4h trend | RSI | ATR% | vs 20d high | vs 20d low | funding/yr | open interest 24h | vol $M | flags"]
    for f in pick:
        flags = []
        if f["breakout"]:
            flags.append("BREAKOUT")
        if f["breakdown"]:
            flags.append("BREAKDOWN")
        if f["coin"] in held:
            flags.append("HELD")
        lines.append(f"{f['coin']} | {f['price']:.6g} | {f['ch_4h']:+.1f}% | {f['ch_24h']:+.1f}% | {f['ch_7d']:+.1f}% | "
                     f"{f['trend']} ({f['trend_gap']:+.1f}%) | {f['rsi']:.0f} | {f['atr_pct']:.1f} | {f['from_high']:+.1f}% | "
                     f"{f['from_low']:+.1f}% | {f['funding_apr']:+.0f}% | "
                     f"{'n/a' if f.get('oi_24h') is None else format(f['oi_24h'], '+.1f') + '%'} | {f['volume_m']:.0f} | {' '.join(flags)}")
    return "\n".join(lines)
