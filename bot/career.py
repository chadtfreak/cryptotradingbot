"""Claude's career: what it earns by doing well and what it loses by doing badly.

Earn your brain: each month's thinking allowance (and access to the smartest model and
news searches) is set by how the previous month went against its two rivals, the maths
bot and simply holding ETH. The owner's monthly cap is the ceiling.

Promotion ladder: a strong 30 days earns a promotion offer. The owner approves it in the
dashboard, which adds to Claude's bankroll. Two losing months in a row put it on probation:
half the risk per trade and one position at a time, until it has a winning month."""

from datetime import datetime, timezone

LEVELS = [("Rookie", 0), ("Trader", 1), ("Senior trader", 2), ("Partner", 4)]  # name, bankrolls added on promotion
PROMOTION_DAYS = 30
PROMOTION_RETURN = 10.0  # percent over the window
PROMOTION_MIN_TRADES = 5
TRIAL_DAYS = 7  # a month with less history than this doesn't count


def month_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def month_bounds(ts: float) -> tuple[int, int]:
    d = datetime.fromtimestamp(ts, timezone.utc)
    start = datetime(d.year, d.month, 1, tzinfo=timezone.utc)
    end = datetime(d.year + d.month // 12, d.month % 12 + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp()), int(end.timestamp())


def window_return(store, start: int, end: int) -> tuple[float | None, float | None, float]:
    """(bot return %, ETH buy-and-hold return %, days covered) between two times, net of deposits."""
    points = [p for p in store.equity_history(start) if p["ts"] < end]
    if len(points) < 2:
        return None, None, 0.0
    first, last = points[0], points[-1]
    deposits = store.ledger_total_since("deposit", first["ts"] + 1) - store.ledger_total_since("deposit", last["ts"] + 1)
    ret = ((last["equity"] - deposits) / first["equity"] - 1) * 100
    hold = (last["price"] / first["price"] - 1) * 100 if first["price"] else None
    return ret, hold, (last["ts"] - first["ts"]) / 86400


class Career:
    def __init__(self, cap: float):
        self.cap = cap

    def level(self, store) -> int:
        return store.get("career_level") or 0

    # Earn your brain
    def allowance(self, store, now: int) -> dict:
        a = store.get("allowance") or {}
        if a.get("month") == month_key(now):
            return a
        return {"month": month_key(now), "amount": self.cap, "smart": True, "why": "First month: full allowance while you prove yourself."}

    def roll_month(self, eng, rival, now: int) -> None:
        """Once a month: grade last month and set this month's allowance and probation."""
        st = eng.store
        key = month_key(now)
        if st.get("career_month") == key:
            return
        st.set("career_month", key)
        start, _ = month_bounds(now)
        prev_start, _ = month_bounds(start - 1)
        ret, hold, days = window_return(st, prev_start, start)
        if ret is None or days < TRIAL_DAYS:
            return
        rival_ret = window_return(rival.store, prev_start, start)[0] if rival is not None else None
        beat_maths = rival_ret is None or ret > rival_ret
        beat_hold = hold is None or ret > hold
        beaten = int(beat_maths) + int(beat_hold)
        if beaten == 2:
            amount, smart, why = self.cap, True, "You beat both the maths bot and holding ETH last month: full allowance and the smart model."
        elif beaten == 1:
            amount, smart, why = round(self.cap * 2 / 3, 2), True, "You beat one of your two rivals last month: two thirds of the allowance."
        else:
            amount, smart, why = round(self.cap * 0.4, 2), False, "You beat neither rival last month: a reduced allowance and the cheaper model only."
        st.set("allowance", {"month": key, "amount": amount, "smart": smart, "why": why})

        streak = (st.get("losing_streak") or 0) + 1 if ret < 0 else 0
        st.set("losing_streak", streak)
        was = bool(st.get("probation"))
        if streak >= 2:
            st.set("probation", True)
        elif ret > 0 and beaten >= 1:
            st.set("probation", False)
        history = st.get("career_history") or []
        history.append({"month": month_key(prev_start), "return": ret, "maths": rival_ret, "hold": hold,
                        "beaten": beaten, "allowance_next": amount})
        st.set("career_history", history[-24:])
        rv = f"{rival_ret:+.1f}%" if rival_ret is not None else "n/a"
        hd = f"{hold:+.1f}%" if hold is not None else "n/a"
        st.log(f"Month graded: me {ret:+.1f}%, maths bot {rv}, holding ETH {hd}. {why} "
               f"This month's thinking allowance: ${amount:.2f}.", level="warning" if beaten < 2 else "info", ts=now)
        if st.get("probation") and not was:
            st.log("Two losing months in a row: I'm on probation. Half risk per trade and one position at a time until I have a winning month.", level="warning", ts=now)
        if was and not st.get("probation"):
            st.log("Off probation after a winning month. Full rules restored.", ts=now)

    # Promotion ladder
    def check_promotion(self, eng, rival, now: int) -> None:
        st = eng.store
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if st.get("promotion_checked") == day or st.get("promotion_offer") or st.get("probation"):
            return
        st.set("promotion_checked", day)
        lvl = self.level(st)
        if lvl + 1 >= len(LEVELS):
            return
        start = now - PROMOTION_DAYS * 86400
        ret, hold, days = window_return(st, start, now + 1)
        if ret is None or days < PROMOTION_DAYS - 1:
            return
        rival_ret = window_return(rival.store, start, now + 1)[0] if rival is not None else None
        closed = sum(1 for t in st.trades(1000) if t["ts"] >= start and t["pnl"] is not None)
        if ret >= PROMOTION_RETURN and (rival_ret is None or ret > rival_ret) and (hold is None or ret > hold) and closed >= PROMOTION_MIN_TRADES:
            name, times = LEVELS[lvl + 1]
            amount = times * eng.bankroll_base
            st.set("promotion_offer", {"level": lvl + 1, "name": name, "amount": amount, "since": now,
                                       "stats": {"return": ret, "maths": rival_ret, "hold": hold, "trades": closed}})
            st.log(f"I've earned a promotion to {name}: {ret:+.1f}% over 30 days, beating both rivals across {closed} trades. "
                   f"Waiting for my owner to approve a ${amount:.0f} bankroll increase.", level="trade", ts=now)

    def approve(self, eng, now: int) -> str:
        st = eng.store
        offer = st.get("promotion_offer")
        if not offer:
            raise ValueError("There's no promotion waiting.")
        venue = (st.get("venue") or {}).get("name", "paper")
        if venue == "mainnet":
            raise ValueError("On real money, add the funds to the Hyperliquid account yourself; automatic top-ups aren't supported yet.")
        room = eng.available_to_add()
        if eng.live and (room is None or room + 0.01 < offer["amount"]):
            raise ValueError(f"Add {offer['amount'] - (room or 0):,.0f} test USDC to the testnet account first: it only has "
                             f"{room or 0:,.2f} spare beyond Claude's bankroll.")
        eng.deposit(now, offer["amount"], f"Promotion to {offer['name']}")
        st.set("career_level", offer["level"])
        st.set("promotion_offer", None)
        st.log(f"Promoted to {offer['name']}! My bankroll grew by ${offer['amount']:.0f}.", level="trade", ts=now)
        return offer["name"]

    def decline(self, eng, now: int) -> None:
        if eng.store.get("promotion_offer"):
            eng.store.set("promotion_offer", None)
            eng.store.log("My owner declined the promotion for now.", ts=now)

    # What Claude and the dashboard see
    def summary(self, store, now: int, base: float = 100.0) -> dict:
        lvl = self.level(store)
        nxt = LEVELS[lvl + 1] if lvl + 1 < len(LEVELS) else None
        return {"level": LEVELS[lvl][0], "next_level": nxt[0] if nxt else None, "next_amount": nxt[1] * base if nxt else None,
                "allowance": self.allowance(store, now), "probation": bool(store.get("probation")),
                "losing_streak": store.get("losing_streak") or 0, "offer": store.get("promotion_offer"),
                "history": store.get("career_history") or []}

    def context(self, store, rival, now: int, base: float = 100.0) -> str:
        c = self.summary(store, now, base)
        a = c["allowance"]
        lines = [f"- Level: {c['level']}."]
        if c["next_level"]:
            start = now - PROMOTION_DAYS * 86400
            ret, hold, days = window_return(store, start, now + 1)
            rival_ret = window_return(rival.store, start, now + 1)[0] if rival is not None else None
            closed = sum(1 for t in store.trades(1000) if t["ts"] >= start and t["pnl"] is not None)
            prog = (f"Last {min(days, PROMOTION_DAYS):.0f} days: you {ret:+.1f}%, maths bot "
                    f"{'n/a' if rival_ret is None else f'{rival_ret:+.1f}%'}, holding ETH {'n/a' if hold is None else f'{hold:+.1f}%'}, "
                    f"{closed} closed trades.") if ret is not None else "Not enough history yet."
            lines.append(f"- Promotion to {c['next_level']} (+${c['next_amount']:,.0f} bankroll) needs, over 30 days: at least "
                         f"+{PROMOTION_RETURN:.0f}%, beating both the maths bot and holding ETH, and {PROMOTION_MIN_TRADES}+ closed trades. {prog}")
        lines.append(f"- This month's thinking allowance: ${a['amount']:.2f}. {a['why']} Next month's allowance depends on "
                     "beating both rivals this month: beat both for the full allowance and the smart model, one for two thirds, "
                     "neither for 40% and the cheaper model only.")
        if c["probation"]:
            lines.append("- You are ON PROBATION after two losing months: half risk per trade and one position at a time until you have a winning month.")
        elif c["losing_streak"]:
            lines.append("- Last month was a losing month. Another one puts you on probation.")
        return "\n".join(lines)
