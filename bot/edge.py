"""Proven edges: backtested setups that have made money, over years of history, in market
conditions like the ones right now.

A setup counts as proven when it has at least 300 trades of history with a positive average, the
conditions it fires in now (BTC's trend, the coin's trend, funding, volatility) averaged at least
+0.08R per trade (each with 100+ trades), and none of those conditions lost money. Everything else
is left to Claude's judgement.

Proven setups can be taken automatically, at a standard size, with the exact stop and target that
were tested. Any proven setup that doesn't end up traded is followed like a shadow call, so the
scorecard shows whether skipping them helps or costs money."""

from . import research

MIN_TRADES = 300
MIN_CELL_TRADES = 100
MIN_EDGE_R = 0.08
FLOOR_R = -0.02
STOP_ATR = 1.5
TARGET_ATR = 3.0
HOLD_HOURS = 120  # the backtest's 5 day limit


def estimate(cells: dict, setup: str, d: int, reg: dict) -> float | None:
    """Expected R per trade for this setup and side in these conditions, or None if it isn't proven."""
    base = cells.get(f"{setup}|{d}")
    if not base or base[0] < MIN_TRADES or base[1] <= 0:
        return None
    found = [cells.get(f"{setup}|{d}|{k}|{v}") for k, v in reg.items() if v]
    found = [c for c in found if c and c[0] >= MIN_CELL_TRADES]
    if len(found) < 2 or any(c[1] < FLOOR_R for c in found):
        return None
    avg = sum(c[1] for c in found) / len(found)
    return avg if avg >= MIN_EDGE_R else None


def firing(feats: list[dict], cells: dict) -> list[dict]:
    """Proven setups firing on the scanner's coins right now, best first."""
    if not cells:
        return []
    btc = next((f for f in feats if f["coin"] == "BTC"), None)
    btc_up = None if btc is None else btc["trend"] == "up"
    out = []
    for f in feats:
        if not f.get("atr"):
            continue
        reg = research.regime(btc_up, f.get("trend_atr", 0), f["funding_apr"], f["atr_pct"], f.get("median_vol", 0))
        for setup, d in f.get("setups") or []:
            est = estimate(cells, setup, d, reg)
            if est is not None:
                out.append({"coin": f["coin"], "setup": setup, "dir": d, "est": est, "atr_pct": f["atr_pct"],
                            "candle_ts": f.get("candle_ts"), "key": f"{f['coin']}|{setup}|{d}|{f.get('candle_ts')}"})
    return sorted(out, key=lambda x: -x["est"])


def levels(entry: float, d: int, atr_pct: float) -> tuple[float, float]:
    """The tested stop and target for a proven setup, scaled to the live price."""
    atr = entry * atr_pct / 100
    return entry - d * STOP_ATR * atr, entry + d * TARGET_ATR * atr


def watch(store, items: list[dict], prices: dict, now: int) -> None:
    """Remember proven setups seen now, to check on the next tick whether they were traded."""
    w = store.get("edge_watch") or []
    w += [{**x, "seen": now, "entry": prices[x["coin"]]} for x in items if prices.get(x["coin"])]
    store.set("edge_watch", w[-50:])


def settle_watch(store, positions: dict, now: int) -> list[dict]:
    """Proven setups from earlier ticks that didn't get traded become skipped-edge calls."""
    w = store.get("edge_watch") or []
    if not w:
        return []
    keep, skipped = [], []
    for x in w:
        if x["seen"] >= now:
            keep.append(x)
            continue
        pos = positions.get(x["coin"])
        if pos and (pos["qty"] > 0) == (x["dir"] > 0):
            continue  # taken
        stop, target = levels(x["entry"], x["dir"], x["atr_pct"])
        skipped.append({"coin": x["coin"], "dir": x["dir"], "entry": x["entry"], "stop": stop, "target": target, "ts": x["seen"],
                        "expires": x["seen"] + HOLD_HOURS * 3600, "prob": 50.0, "setup": x["setup"], "est": x["est"]})
    store.set("edge_watch", keep)
    if skipped:
        store.set("skipped_edges", ((store.get("skipped_edges") or []) + skipped)[-50:])
    return skipped
