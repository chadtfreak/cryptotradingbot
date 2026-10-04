"""A weekly report for the owner, like the one-pager a fund sends its investors. Built by code
from the bot's own records every Monday (UTC), so it costs nothing."""

from datetime import datetime, timedelta, timezone

from .career import window_return

KEEP = 12


def week_start(ts: int) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    monday = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - timedelta(days=d.weekday())
    return int(monday.timestamp())


def build(eng, rival, start: int, end: int) -> dict:
    st = eng.store
    ret, hold, days = window_return(st, start, end)
    rival_ret = window_return(rival.store, start, end)[0] if rival is not None else None
    points = [p for p in st.equity_history(start) if p["ts"] < end]
    worst = 0.0
    if points:  # deepest drop from a high within the week, with top-ups taken out
        peak = None
        for p in points:
            value = p["equity"] - (st.ledger_total_since("deposit", start) - st.ledger_total_since("deposit", p["ts"] + 1))
            peak = value if peak is None else max(peak, value)
            worst = max(worst, 1 - value / peak if peak else 0)
    trades = [t for t in st.get("trade_records") or [] if start <= t["closed"] < end]
    rs = [t["r"] for t in trades if t.get("r") is not None]
    by_setup = {}
    for t in trades:
        b = by_setup.setdefault(t["setup"], {"n": 0, "pnl": 0.0, "r": []})
        b["n"] += 1
        b["pnl"] += t["pnl"]
        if t.get("r") is not None:
            b["r"].append(t["r"])
    calls = [c for c in st.get("shadow_results") or [] if start <= c["closed"] < end]
    thinking = -(st.ledger_total_since("ai_cost", start) - st.ledger_total_since("ai_cost", end)
                 + st.ledger_total_since("ai_sponsored", start) - st.ledger_total_since("ai_sponsored", end))
    review = next((d for d in st.decisions(50, kind="review", since=start) if d["ts"] < end), None)
    lessons = [x for x in st.get("trade_lessons") or [] if start <= x["ts"] < end]
    best = max(trades, key=lambda t: t["pnl"], default=None)
    worst_trade = min(trades, key=lambda t: t["pnl"], default=None)
    checks = len([d for d in st.decisions(1000, kind="decision", since=start) if d["ts"] < end])
    r = {
        "start": start, "end": end, "days": days,
        "equity_start": points[0]["equity"] if points else None, "equity_end": points[-1]["equity"] if points else None,
        "return": ret, "maths": rival_ret, "hold": hold, "max_drawdown": worst * 100,
        "trades": len(trades), "win_rate": sum(1 for x in rs if x > 0) / len(rs) * 100 if rs else None,
        "avg_r": sum(rs) / len(rs) if rs else None, "pnl": sum(t["pnl"] for t in trades),
        "best": best and {"coin": best["coin"], "pnl": best["pnl"], "r": best.get("r")},
        "worst": worst_trade and {"coin": worst_trade["coin"], "pnl": worst_trade["pnl"], "r": worst_trade.get("r")},
        "by_setup": {k: {"n": v["n"], "pnl": v["pnl"], "avg_r": sum(v["r"]) / len(v["r"]) if v["r"] else None} for k, v in by_setup.items()},
        "calls": len(calls), "calls_right": sum(1 for c in calls if c["r"] > 0) / len(calls) * 100 if calls else None,
        "thinking": thinking, "checks": checks,
        "review": review["reasoning"] if review else None, "lessons": [x["lesson"] for x in lessons][-5:],
    }
    r["summary"] = summary(r)
    return r


def summary(r: dict) -> str:
    pct = lambda x: "n/a" if x is None else f"{x:+.1f}%"
    parts = [f"{pct(r['return'])} for the week, against {pct(r['maths'])} for the maths bot and {pct(r['hold'])} for holding ETH."
             if r["return"] is not None else "Not enough history yet to measure a return."]
    if r["return"] is not None:
        rivals = [x for x in (r["maths"], r["hold"]) if x is not None]
        beat = sum(1 for x in rivals if r["return"] > x)
        parts.append(f"Beat {beat} of {len(rivals)} rivals." if rivals else "")
    if r["trades"]:
        parts.append(f"{r['trades']} trades closed, {r['win_rate']:.0f}% winners, {r['avg_r']:+.2f}R average, {r['pnl']:+.2f} USDC."
                     if r["avg_r"] is not None else f"{r['trades']} trades closed, {r['pnl']:+.2f} USDC.")
    else:
        parts.append("No trades closed.")
    parts.append(f"Worst drop {r['max_drawdown']:.1f}%. {r['checks']} checks, ${r['thinking']:.2f} of thinking.")
    return " ".join(p for p in parts if p)


def maybe_build(eng, rival, now: int) -> dict | None:
    """Builds last week's report once, early each Monday (UTC)."""
    st = eng.store
    this_week = week_start(now)
    if st.get("last_report_week") == this_week:
        return None
    st.set("last_report_week", this_week)
    born = st.get("started_at") or now
    if born >= this_week:
        return None  # nothing to report yet
    start = max(this_week - 7 * 86400, born)
    rep = build(eng, rival, start, this_week)
    st.set("weekly_reports", ((st.get("weekly_reports") or []) + [rep])[-KEEP:])
    st.log(f"Weekly report ready: {rep['summary']}", ts=now)
    return rep
