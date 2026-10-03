"""Fast feedback, so Claude learns in days rather than months.

* Shadow calls: every time Claude wakes it also makes a few quick calls on coins it may or
  may not trade (direction, stop, target, how sure it is). Code marks them against live
  prices for free, so each wake produces several graded results instead of one trade a week.
* Trade records: every closed trade is kept with its setup and its result in R (multiples of
  what was risked), giving Claude its own stats per setup.
* Trade reviews: right after a trade closes, a cheap model looks at what happened and writes
  one lesson, instead of waiting for the weekly review.
"""

import json
from datetime import datetime, timezone

MAX_OPEN_CALLS = 20
MAX_CALLS_PER_WAKE = 5
KEEP_RESULTS = 300
KEEP_LESSONS = 30

CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "coin": {"type": "string"},
        "direction": {"type": "string", "enum": ["long", "short"]},
        "stop_price": {"type": "number", "description": "Where the call is wrong."},
        "target_price": {"type": "number", "description": "Where the call is right."},
        "hours": {"type": "number", "description": "How long the call has to work, 4 to 72 hours. Marked on the price at the end if neither is hit."},
        "probability": {"type": "number", "description": "Your honest chance (0 to 100) that the target is hit before the stop."},
        "setup": {"type": "string", "description": "breakout, pullback, range_fade, squeeze_fade, failed_breakout, momentum or other."},
    },
    "required": ["coin", "direction", "stop_price", "target_price", "hours", "probability", "setup"],
    "additionalProperties": False,
}

TRADE_REVIEW_PROMPT = """You are the trading mind of Survival Bot. One of your trades just closed. Look at why you took it, what price did while you held it, and how it ended. Be an honest coach: separate the quality of the decision from the result. A good decision can lose and a bad one can win. Then write ONE short, specific lesson you can use next time (one sentence, under 40 words). Call submit_trade_review once."""

TRADE_REVIEW_TOOL = {
    "name": "submit_trade_review",
    "description": "Your verdict and one lesson from this trade. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["good trade", "good decision, unlucky", "mistake", "lucky"]},
            "lesson": {"type": "string"},
        },
        "required": ["verdict", "lesson"],
        "additionalProperties": False,
    },
}


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %H:%M UTC")


def px(p: float) -> str:
    return f"{p:,.2f}" if p >= 100 else f"{p:.6g}"


# Shadow calls
def add_calls(store, calls: list[dict], prices: dict, now: int) -> list[str]:
    """Saves valid calls and returns a short label for each."""
    book = store.get("shadow_calls") or []
    labels = []
    for c in (calls or [])[:MAX_CALLS_PER_WAKE]:
        coin = str(c.get("coin") or "").upper().strip()
        entry = prices.get(coin)
        d = 1 if c.get("direction") == "long" else -1
        stop, target = c.get("stop_price"), c.get("target_price")
        if not entry or not stop or not target:
            continue
        if d * (entry - stop) <= 0 or d * (target - entry) <= 0:
            continue  # stop or target on the wrong side of the price
        hours = min(max(float(c.get("hours") or 24), 4), 72)
        book.append({"coin": coin, "dir": d, "entry": entry, "stop": stop, "target": target, "ts": now,
                     "expires": now + int(hours * 3600), "prob": min(max(float(c.get("probability") or 50), 0), 100),
                     "setup": c.get("setup") or "other"})
        labels.append(f"{coin} {'up' if d > 0 else 'down'}")
    store.set("shadow_calls", book[-MAX_OPEN_CALLS:])
    return labels


def grade_calls(store, prices: dict, now: int) -> list[dict]:
    """Marks open calls against the latest prices. Returns the ones that just resolved."""
    book = store.get("shadow_calls") or []
    if not book:
        return []
    still, done = [], []
    for c in book:
        p = prices.get(c["coin"])
        if p is None:
            still.append(c)
            continue
        d, risk = c["dir"], abs(c["entry"] - c["stop"])
        if d * (p - c["stop"]) <= 0:
            r, how = -1.0, "stop"
        elif d * (p - c["target"]) >= 0:
            r, how = abs(c["target"] - c["entry"]) / risk, "target"
        elif now >= c["expires"]:
            r, how = d * (p - c["entry"]) / risk, "time"
        else:
            still.append(c)
            continue
        done.append({**c, "r": r, "exit": how, "closed": now, "exit_price": p})
    if done:
        store.set("shadow_calls", still)
        store.set("shadow_results", ((store.get("shadow_results") or []) + done)[-KEEP_RESULTS:])
    return done


# Trade records
def note_open(pos: dict, qty: float, price: float, stop: float) -> None:
    """Adds this fill's risk to the position, so results can be measured in R."""
    pos["risk_usd"] = (pos.get("risk_usd") or 0.0) + abs(qty) * abs(price - stop)


def record_close(store, coin: str, pos: dict, pnl: float, fraction: float, now: int, exit_price: float, reason: str) -> dict | None:
    """Adds up partial closes and returns the finished trade when the position is fully closed."""
    running = store.get("open_trade_pnl") or {}
    total = running.get(coin, 0.0) + pnl
    if fraction < 1:
        running[coin] = total
        store.set("open_trade_pnl", running)
        return None
    running.pop(coin, None)
    store.set("open_trade_pnl", running)
    risk = pos.get("risk_usd") or 0.0
    qty = pos.get("qty") or 0.0
    rec = {"coin": coin, "side": "long" if qty > 0 else "short", "setup": pos.get("setup") or "other",
           "opened": pos.get("opened_at"), "closed": now, "pnl": total, "r": total / risk if risk > 0 else None,
           "entry": abs(pos["cost_basis"] / qty) if qty else None, "exit": exit_price, "reason": reason[:160]}
    store.set("trade_records", ((store.get("trade_records") or []) + [rec])[-KEEP_RESULTS:])
    pending = store.get("trade_reviews_pending") or []
    store.set("trade_reviews_pending", (pending + [rec])[-5:])
    return rec


# What Claude and the dashboard see
def _group(rows, key):
    out = {}
    for r in rows:
        out.setdefault(r.get(key) or "other", []).append(r)
    return out


def stats(store) -> dict:
    calls = store.get("shadow_results") or []
    trades = store.get("trade_records") or []

    def summary(rows):
        rs = [r["r"] for r in rows if r.get("r") is not None]
        return {"n": len(rows), "win": sum(1 for x in rs if x > 0) / len(rs) * 100 if rs else None,
                "avg_r": sum(rs) / len(rs) if rs else None, "total_r": sum(rs) if rs else 0.0,
                "pnl": sum(r.get("pnl") or 0 for r in rows)}

    calib = []
    for lo, hi in ((0, 45), (45, 60), (60, 75), (75, 101)):
        b = [c for c in calls if lo <= c["prob"] < hi]
        if b:
            calib.append({"range": f"{lo} to {min(hi, 100)}%", "n": len(b), "said": sum(c["prob"] for c in b) / len(b),
                          "hit": sum(1 for c in b if c["exit"] == "target") / len(b) * 100})
    return {"calls": summary(calls), "calls_by_setup": {k: summary(v) for k, v in _group(calls, "setup").items()},
            "calibration": calib, "trades": summary(trades),
            "trades_by_setup": {k: summary(v) for k, v in _group(trades, "setup").items()},
            "open_calls": len(store.get("shadow_calls") or []),
            "recent_calls": list(reversed(calls[-8:])), "lessons": list(reversed((store.get("trade_lessons") or [])[-8:]))}


def scorecard(store) -> str:
    s = stats(store)
    f = lambda x: "n/a" if x is None else f"{x:+.2f}R"
    w = lambda x: "n/a" if x is None else f"{x:.0f}%"
    lines = []
    t = s["trades"]
    if t["n"]:
        lines.append(f"Real trades: {t['n']} closed, {w(t['win'])} winners, {f(t['avg_r'])} average, {t['pnl']:+.2f} USDC in total.")
        for k, v in sorted(s["trades_by_setup"].items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"- {k}: {v['n']} trades, {w(v['win'])} winners, {f(v['avg_r'])} average")
    else:
        lines.append("Real trades: none closed yet.")
    c = s["calls"]
    if c["n"]:
        lines.append(f"Shadow calls: {c['n']} marked, {w(c['win'])} right, {f(c['avg_r'])} average, {c['total_r']:+.1f}R in total. {s['open_calls']} still open.")
        for k, v in sorted(s["calls_by_setup"].items(), key=lambda kv: -kv[1]["n"]):
            lines.append(f"- {k}: {v['n']} calls, {w(v['win'])} right, {f(v['avg_r'])} average")
        if s["calibration"]:
            lines.append("How sure you said you were vs how often the target was hit: " + "; ".join(
                f"{b['range']}: said {b['said']:.0f}%, hit {b['hit']:.0f}% ({b['n']})" for b in s["calibration"]))
        lines.append("Latest marked calls:")
        for r in s["recent_calls"][:5]:
            lines.append(f"- {r['coin']} {'long' if r['dir'] > 0 else 'short'} ({r['setup']}, you said {r['prob']:.0f}%) from {px(r['entry'])}: "
                         f"{r['exit']} at {px(r['exit_price'])}, {r['r']:+.2f}R")
    else:
        lines.append(f"Shadow calls: none marked yet. {s['open_calls']} open.")
    if s["lessons"]:
        lines.append("Lessons from your latest closed trades (newest first):")
        lines += [f"- {x['coin']} {x['setup']} ({x['verdict']}, {x['pnl']:+.2f}): {x['lesson']}" for x in s["lessons"]]
    return "\n".join(lines)


# Trade reviews
def review_trades(brain, eng, key: str, now: int, model: str, limit: int = 2) -> None:
    st = eng.store
    pending = st.get("trade_reviews_pending") or []
    if not pending:
        return
    st.set("trade_reviews_pending", pending[limit:])
    for rec in pending[:limit]:
        try:
            _review_one(brain, eng, key, now, model, rec)
        except Exception as exc:
            st.log(f"Couldn't review the {rec['coin']} trade ({type(exc).__name__}). Skipping it.", level="warning", ts=now)


def _review_one(brain, eng, key, now, model, rec) -> None:
    st = eng.store
    opened = rec.get("opened") or rec["closed"]
    why = next((d for d in st.decisions(200, kind="decision") if d["ts"] <= opened and d["action"] and rec["coin"] in d["action"]), None)
    lines = [f"Trade: {rec['side']} {rec['coin']}, setup {rec['setup']}. Opened {fmt_time(opened)}, closed {fmt_time(rec['closed'])}.",
             f"Entry {px(rec['entry']) if rec['entry'] else '?'}, exit {px(rec['exit'])}. Result {rec['pnl']:+.2f} USDC"
             + (f" ({rec['r']:+.2f}R)." if rec.get("r") is not None else "."),
             f"How it closed: {rec['reason']}"]
    if why:
        lines.append(f"Why you took it: {why['reasoning']} Your note to self: {why['journal'] or ''}")
    try:
        hours = max(2, int((rec["closed"] - opened) / 3600) + 2)
        candles = [c for c in eng.market.candles(60, coin=rec["coin"], count=min(hours, 120)) if c.ts >= opened - 3600]
        lines.append("Price while you held it, 1h candles (UTC, low-high, close): " + " | ".join(
            f"{datetime.fromtimestamp(c.ts, timezone.utc).strftime('%d %H:%M')} {c.low:.6g}-{c.high:.6g} {c.close:.6g}" for c in candles[-72:]))
    except Exception:
        pass
    content, cost, answered_by = brain._call(key, model, TRADE_REVIEW_PROMPT, "\n".join(lines), [TRADE_REVIEW_TOOL])
    brain.pay(eng, now, cost, f"{answered_by}: review of the {rec['coin']} trade")
    call = next((b for b in content if getattr(b, "type", None) == "tool_use" and b.name == "submit_trade_review"), None)
    if call is None:
        return
    r = call.input if isinstance(call.input, dict) else json.loads(call.input)
    lesson = {"coin": rec["coin"], "setup": rec["setup"], "pnl": rec["pnl"], "verdict": r["verdict"], "lesson": r["lesson"], "ts": now}
    st.set("trade_lessons", ((st.get("trade_lessons") or []) + [lesson])[-KEEP_LESSONS:])
    st.add_decision(now, "trade_review", answered_by, f"{rec['coin']} trade closed", r["verdict"], None, r["lesson"], None, cost, None)
    st.log(f"Looked back at the {rec['coin']} {rec['side']} ({rec['pnl']:+.2f} USDC, cost ${cost:.3f}): {r['verdict']}. Lesson: {r['lesson']}",
           level="thought", ts=now)
