"""The go-live checklist: clear pass marks Claude has to hit on testnet before real money is
worth considering. It only reports; unlocking real money stays a deliberate decision by the owner."""

from .career import window_return

DAYS = 60
TRADES = 50
MAX_DRAWDOWN = 0.20


def check(eng, rival, now: int) -> dict:
    st = eng.store
    start = st.get("started_at") or now
    days = (now - start) / 86400
    trades = len(st.get("trade_records") or [])
    equity = eng.equity() if eng.prices else None
    profit = equity - eng.contributed if equity is not None else None
    worst = st.get("max_drawdown")
    ret, hold, _ = window_return(st, start, now + 1)
    rival_ret = window_return(rival.store, start, now + 1)[0] if rival is not None else None
    pct = lambda x: "n/a" if x is None else f"{x:+.1f}%"
    items = [
        {"name": f"Trading for {DAYS}+ days", "have": f"{days:.0f} days", "ok": days >= DAYS, "progress": min(days / DAYS, 1)},
        {"name": f"{TRADES}+ closed trades", "have": f"{trades}", "ok": trades >= TRADES, "progress": min(trades / TRADES, 1)},
        {"name": "Making money after every cost", "have": "n/a" if profit is None else f"{profit:+,.2f} USDC",
         "ok": profit is not None and profit > 0, "progress": None},
        {"name": f"Worst drop under {MAX_DRAWDOWN:.0%}", "have": "n/a" if worst is None else f"{worst * 100:.1f}% so far",
         "ok": worst is not None and worst < MAX_DRAWDOWN, "progress": None},
        {"name": "Beating the maths bot and holding ETH", "have": f"me {pct(ret)}, maths {pct(rival_ret)}, ETH {pct(hold)}",
         "ok": ret is not None and (rival_ret is None or ret > rival_ret) and (hold is None or ret > hold), "progress": None},
    ]
    return {"items": items, "passed": sum(i["ok"] for i in items), "total": len(items)}


def context(result: dict) -> str:
    left = [f"{i['name']} (now {i['have']})" for i in result["items"] if not i["ok"]]
    text = f"- Path to real money: {result['passed']} of {result['total']} checks met."
    if left:
        text += " Still to do: " + "; ".join(left) + "."
    else:
        text += " All met: your owner can now consider real money."
    return text
