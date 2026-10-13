"""Attention before price: does a coin people suddenly start searching for go on to move?

An idea borrowed from Chris Camillo's "social arbitrage": spot rising attention before the price
reflects it. Every hour the bot reads CoinGecko's list of the most searched-for coins. When a coin
that trades on Hyperliquid newly appears on the list while its price is still quiet (under 5% in
24 hours), the bot notes the price and follows what happens over the next 24 and 72 hours.

It's a measurement, not a trading rule: Claude sees the list and the running results, and can act
on them if they show attention really does lead price."""

QUIET_PCT = 5.0
CHECK_EVERY = 3600
KEEP = 200


def hl_name(symbol: str, names: set[str]) -> str | None:
    """The Hyperliquid perp for a CoinGecko symbol, if there is one (Hyperliquid lists some meme coins in thousands, e.g. kPEPE)."""
    for cand in (symbol, f"k{symbol}"):
        if cand in names:
            return cand
    return None


def refresh(store, macro, universe: list[dict], liquid: set[str], now: int) -> list[dict] | None:
    """Reads the trending list (hourly) and starts following coins that newly appear while still quiet."""
    checked = store.get("trending_checked")
    if checked is not None and now - checked < CHECK_EVERY:
        return None
    store.set("trending_checked", now)
    try:
        rows = macro.trending()
    except Exception:
        rows = []
    if not rows:
        return None
    by_name = {r["coin"]: r for r in universe}
    previous = store.get("trending_last")
    last = set(previous or [])
    watch = store.get("attention_watch") or []
    following = {w["coin"] for w in watch}
    out = []
    for r in rows:
        coin = hl_name(r["symbol"], set(by_name))
        u = by_name.get(coin) if coin else None
        ch = (u["mark"] / u["prev_day"] - 1) * 100 if u and u.get("prev_day") else None
        new = previous is not None and r["symbol"] not in last  # the first look is only a baseline
        row = {**r, "coin": coin, "liquid": coin in liquid if coin else False, "hl_change_24h": ch, "new": new}
        out.append(row)
        if new and coin and ch is not None and abs(ch) < QUIET_PCT and coin not in following and u.get("mark"):
            watch.append({"coin": coin, "symbol": r["symbol"], "ts": now, "entry": u["mark"], "ch_before": ch,
                          "rank": r["rank"], "hi": u["mark"], "lo": u["mark"], "liquid": row["liquid"]})
    store.set("trending_last", [r["symbol"] for r in rows])
    store.set("attention_watch", watch[-50:])
    store.set("trending_now", out)
    return out


def settle(store, prices: dict, now: int) -> list[dict]:
    """Follows each watched coin's price; records the 24h and 72h results."""
    watch = store.get("attention_watch") or []
    if not watch:
        return []
    still, done, changed = [], [], False
    for w in watch:
        p = prices.get(w["coin"])
        if p is not None:
            if p > w["hi"] or p < w["lo"]:
                w["hi"], w["lo"], changed = max(w["hi"], p), min(w["lo"], p), True
            if "ret_24h" not in w and now - w["ts"] >= 86400:
                w["ret_24h"], changed = (p / w["entry"] - 1) * 100, True
            if now - w["ts"] >= 3 * 86400:
                done.append({**w, "ret_72h": (p / w["entry"] - 1) * 100, "max_up": (w["hi"] / w["entry"] - 1) * 100,
                             "max_down": (1 - w["lo"] / w["entry"]) * 100, "closed": now})
                continue
        still.append(w)
    if done or changed:
        store.set("attention_watch", still)
    if done:
        store.set("attention_results", ((store.get("attention_results") or []) + done)[-KEEP:])
    return done


def stats(store) -> dict:
    res = store.get("attention_results") or []
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    return {"n": len(res), "open": len(store.get("attention_watch") or []),
            "avg_24h": avg([r["ret_24h"] for r in res if "ret_24h" in r]), "avg_72h": avg([r["ret_72h"] for r in res]),
            "up_72h": sum(1 for r in res if r["ret_72h"] > 0) / len(res) * 100 if res else None,
            "avg_max_up": avg([r["max_up"] for r in res]), "avg_max_down": avg([r["max_down"] for r in res])}


def context(store) -> str | None:
    rows = store.get("trending_now") or []
    if not rows:
        return None
    lines = ["## Trending on CoinGecko (the coins people are searching for most right now)",
             "An attention signal, not a trade signal: by the time a coin is everywhere, the move is often done. "
             "The interesting ones are coins that newly appear while their price is still quiet."]
    for r in rows:
        if not r["coin"]:
            continue  # not tradeable on Hyperliquid
        ch = "n/a" if r["hl_change_24h"] is None else f"{r['hl_change_24h']:+.1f}%"
        flags = [f for f in ("NEW" if r["new"] else "", "quiet price" if r["hl_change_24h"] is not None and abs(r["hl_change_24h"]) < QUIET_PCT else "",
                             "" if r["liquid"] else "below your volume limit") if f]
        lines.append(f"- #{r['rank']} {r['symbol']} ({r['coin']} on Hyperliquid): 24h {ch}" + (f" [{', '.join(flags)}]" if flags else ""))
    others = [r["symbol"] for r in rows if not r["coin"]]
    if others:
        lines.append(f"Also trending but not on Hyperliquid: {', '.join(others)}.")
    s = stats(store)
    if s["n"]:
        lines.append(f"Does attention lead price? {s['n']} coins that started trending while quiet have been followed for 3 days: "
                     f"average {s['avg_24h']:+.1f}% after 24h and {s['avg_72h']:+.1f}% after 72h, up in {s['up_72h']:.0f}% of cases "
                     f"(average best move +{s['avg_max_up']:.1f}%, worst -{s['avg_max_down']:.1f}%). {s['open']} still being followed.")
    else:
        lines.append(f"Does attention lead price? No results yet: {s['open']} coins being followed for 3 days.")
    return "\n".join(lines) + "\n"
