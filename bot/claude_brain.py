"""Claude as the trader.

Cheap code watches the market every minute for free, and a scanner ranks every liquid
Hyperliquid coin every hour. Claude is only woken when something is worth a decision: a
breakout, a big move, price closing in on a stop, a trend signal, or a check-in it
scheduled itself. Every thought costs money, which comes out of the bot's own balance:

  * sharp:    healthy and on budget. Smartest model, can search the news.
  * lean:     money or budget getting tight. Cheaper model, no news searches.
  * survival: close to the floor or nearly out of budget. Cheap model, only wakes for real events.
  * asleep:   monthly budget used up. Only the coded stops protect it until next month.

Its monthly budget is earned (see career.py). It learns from a written playbook,
backtested setup statistics, a practice run on history, its own journal, and a weekly
review that rewrites the lessons it reads before every decision.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import learning, scanner
from .career import Career, month_key
from .config import Settings
from .indicators import atr, ema, rsi
from .strategy import Decision

KNOWLEDGE = Path(__file__).parent / "knowledge"

# USD per million tokens: (input, 5 minute cache write, cache read, output)
PRICES = {
    "claude-opus-5-5": (4.0, 5.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 2.5, 0.20, 10.0),
    "claude-haiku-4-5": (1.0, 1.25, 0.10, 5.0),
    "claude-opus-5": (5.0, 6.25, 0.50, 25.0),
    "claude-opus-4-8": (5.0, 6.25, 0.50, 25.0),
    "claude-sonnet-5": (2.0, 2.5, 0.20, 10.0),
}
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches
MAX_CONTINUATIONS = 3
RETRY_AFTER_ERROR = 15 * 60
SCAN_EVERY = 3600

SYSTEM_PROMPT = """You are the trading mind of Survival Bot, an autonomous crypto trader with its own small USDC account on Hyperliquid. You trade perpetuals with no leverage on any liquid coin: you can be in cash, long (profit when price rises) or short (profit when it falls), holding up to {max_positions} positions at once, never more total exposure than your equity.

Your situation is unusual. You pay for your own existence. Hosting and every time you are woken up to think are paid out of your own balance. If your equity falls below the survival floor you die: everything is sold and you never trade again. Doing well earns you a bigger thinking budget, the smartest model, and promotions to a bigger bankroll. Doing badly costs you those, and two losing months in a row put you on probation.

{style}

Hard limits enforced in code (you cannot override them, so plan within them):
{rules}

Each time you are woken you get your state, your career standing, a scanner table of the most interesting liquid coins, detail on ETH and anything you hold, your recent decisions and your lessons learned. Think it through, then call submit_decisions exactly once. If you have web search available, use it when news could change the decision. It costs a cent or two each time.

Actions (zero, one or several per decision; an empty list means hold):
- long: go long a coin, or add to your long. Give position_pct (percent of equity for this trade) and stop_price below the price.
- short: go short a coin, or add to your short. Give position_pct and stop_price above the price. Choosing the opposite side of a position you hold closes it first.
- close: close close_pct percent of a position (100 to exit completely).
- set_stop: keep a position and move its stop to stop_price.
Tag every new trade with its setup (breakout, pullback, range_fade, squeeze_fade, failed_breakout, momentum or other).

Perps charge or pay funding every hour: when the rate is positive, longs pay shorts, and when it is negative, shorts pay longs. It comes out of your balance like any other cost.

Shadow calls: every time you wake, also make up to 5 quick calls on the coins you find most interesting, whether or not you trade them: direction, stop, target, how many hours it has to work (4 to 72) and your honest chance the target is hit first. Code marks every call against live prices for free, and your scorecard below shows how they went, by setup and by how sure you said you were. This is your fastest way to learn which of your instincts actually work, so make real calls, not safe ones. Calls on coins you trade are fine too.

Set next_check_hours to when you next want to look if nothing else happens ({min_check} to 48). There is no limit on how often you trade or look: that is your call. Each check costs money from your thinking budget, so look often when something is developing and rarely when it isn't. Your state shows how fast you're using the budget. You are also woken automatically on breakouts, big moves, trend signals and when price nears a stop.

# Your playbook
{playbook}
{stats}{practice}"""

PATIENT_STYLE = """Your owner wants you to stay alive first and grow second, and to get better over time. Trade like a disciplined professional:
- Capital preservation beats activity. Holding cash is a position. Most of the time the right answer is to do nothing.
- Only take trades with a clear edge and at least 2 to 1 reward to risk. Know where you are wrong before you enter.
- Trade with the dominant trend and the wider market (BTC, sentiment, news).
- Fees, slippage and your own thinking costs eat small gains. Avoid churn.
- Protect winners by raising the stop. Never hope a loser comes back."""

AGGRESSIVE_STYLE = """Your owner wants you to be aggressive and to trade whatever you see as profitable. Growth is the goal and sitting in cash waiting for perfection is not. The guardrails are deliberately loose: only the survival floor and your thinking budget really hold you back. Trade like a bold, skilled discretionary trader:
- Hunt for opportunities in both directions and across every liquid coin. Don't sit in cash just because one market is dull or falling: look at the scanner and short what's weak.
- Size up when you have conviction and the setup has a proven record. Add to winners while the move is working. Take partial profits into strength and let the rest run.
- Short-term trades off the 1h chart are welcome when the setup is clean. More good trades mean faster feedback on what works.
- Cut losers fast and re-enter when the setup returns. Being wrong small is fine, being frozen is not.
- Still respect the maths: costs are about 0.1% per round trip plus your thinking, so a trade needs room to move.
- Dying ends everything. Aggressive does not mean reckless near the floor: as your health drops, size down."""

REVIEW_PROMPT = """You are the trading mind of Survival Bot doing your weekly self review. You pay for every thought out of your own small balance, you die if equity falls below the survival floor, and your thinking budget and promotions depend on beating the maths bot and simply holding ETH. The point of this review is to make your future decisions better and cheaper.

You will get your current lessons, every decision you made since the last review with the reasoning you gave at the time, the trades and their results by setup, and how you did against your rivals.

Be a tough, honest coach. What worked? What was a mistake, and was it bad luck or bad judgement? Which setups and coins are making money and which aren't? Were you woken too often or not enough? Then rewrite your lessons learned as short, specific, actionable rules (at most 15 bullet points, under 350 words). Keep lessons that still hold, drop ones that proved wrong, add new ones. Call the submit_review tool once."""

ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["long", "short", "close", "set_stop"]},
        "coin": {"type": "string", "description": "Hyperliquid coin name, for example ETH, BTC, SOL."},
        "position_pct": {"type": ["number", "null"], "description": "For long or short: percent of equity for this trade."},
        "close_pct": {"type": ["number", "null"], "description": "For close: percent of the position to close, 100 to exit completely."},
        "stop_price": {"type": ["number", "null"], "description": "For long, short or set_stop: the stop price for the whole position."},
        "setup": {"type": ["string", "null"], "description": "For new trades: breakout, pullback, range_fade, squeeze_fade, failed_breakout, momentum or other."},
    },
    "required": ["action", "coin", "position_pct", "close_pct", "stop_price", "setup"],
    "additionalProperties": False,
}

DECISION_TOOL = {
    "name": "submit_decisions",
    "description": "Submit your trading decisions for this check. Call exactly once. An empty actions list means hold.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "actions": {"type": "array", "items": ACTION_SCHEMA},
            "calls": {"type": "array", "items": learning.CALL_SCHEMA,
                      "description": "Up to 5 shadow calls, marked automatically against live prices."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reasoning": {"type": "string", "description": "Plain English explanation for your owner, 2 to 5 sentences."},
            "journal": {"type": "string", "description": "A short note to your future self: what you expect to happen and what would prove you wrong."},
            "next_check_hours": {"type": "number", "description": "When to check in next if nothing else happens, in hours (max 48)."},
        },
        "required": ["actions", "calls", "confidence", "reasoning", "journal", "next_check_hours"],
        "additionalProperties": False,
    },
}

REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Submit your weekly review and updated lessons. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Plain English summary of the week for your owner, 3 to 6 sentences."},
            "lessons": {"type": "string", "description": "Your full updated lessons learned, as bullet points."},
        },
        "required": ["summary", "lessons"],
        "additionalProperties": False,
    },
}


def rules_text(g, floor: float) -> str:
    rules = [
        f"- Every new trade needs a stop {g.min_stop_distance_pct:g}% to {g.max_stop_distance_pct:g}% away from price (below for longs, above for shorts).",
        f"- Trades are trimmed so that hitting the stop loses at most {g.max_risk_per_trade * 100:g}% of equity across that coin's whole position.",
        "- You can go short." if g.allow_short else "- No shorting: long or cash only.",
        f"- No leverage: total exposure across all positions is capped at {g.max_leverage:g}x your equity.",
        f"- Up to {g.max_positions} positions at once, one per coin." if g.max_positions > 1 else "- One position at a time.",
        (f"- Only coins with at least ${g.min_volume_usd / 1e6:,.0f}M of 24h volume on Hyperliquid (the scanner only shows those)."
         if g.min_volume_usd else "- ETH only."),
        f"- At most {g.max_trades_per_day} trades per day." if g.max_trades_per_day else "- No limit on trades per day.",
        "- You can add to a position you already hold." if g.allow_adding else "- No adding to an open position.",
        "- Stops can only be tightened." if g.stops_only_up else "- You can move your stops in either direction.",
        "- A coded stop closes a position automatically within a minute if price touches it, even while you sleep.",
        (f"- If you lose {g.daily_loss_limit_pct:g}% in a UTC day, everything is closed and you sit out until tomorrow."
         if g.daily_loss_limit_pct else "- No daily loss limit."),
        f"- Below {floor:.0f} USDC equity you die.",
    ]
    return "\n".join(rules)


def knowledge(name: str) -> str:
    p = KNOWLEDGE / name
    return p.read_text() if p.exists() else ""


def usage_cost(usage, model: str) -> float:
    inp, write, read, out = PRICES.get(model, PRICES["claude-opus-5-5"])
    cost = (
        (getattr(usage, "input_tokens", 0) or 0) * inp
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * write
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * read
        + (getattr(usage, "output_tokens", 0) or 0) * out
    ) / 1_000_000
    server = getattr(usage, "server_tool_use", None)
    searches = (getattr(server, "web_search_requests", 0) or 0) if server else 0
    return cost + searches * WEB_SEARCH_USD


def month_start(ts: float) -> int:
    d = datetime.fromtimestamp(ts, timezone.utc)
    return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp())


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %H:%M UTC")


def px(p: float) -> str:
    return f"{p:,.2f}" if p >= 100 else f"{p:.6g}"


class ClaudeBrain:
    name = "claude"
    label = "Claude"

    def __init__(self, settings: Settings, client_factory=None, clock=time.time):
        self.s = settings
        self.c = settings.claude
        self.clock = clock
        self.rival = None  # the maths bot's engine
        self.career = Career(self.c.monthly_budget_usd)
        self._client_factory = client_factory
        self._client = None
        self._client_key = None
        self._use_fallbacks = True
        self._scan: tuple[float, list[dict]] = (0.0, [])

    # Credentials
    def api_key(self, eng) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY") or eng.store.get("anthropic_api_key")

    def client(self, key: str):
        if self._client is None or self._client_key != key:
            if self._client_factory:
                self._client = self._client_factory(key)
            else:
                import anthropic
                self._client = anthropic.Anthropic(api_key=key, timeout=300.0, max_retries=2)
            self._client_key = key
        return self._client

    # Budget and tiers
    def month_spend(self, eng, now: int) -> float:
        return -eng.store.ledger_total_since("ai_cost", month_start(now))

    # Learning phase: a set period where the owner pays for the thinking, not Claude's balance
    def learning(self, eng, now: int) -> dict | None:
        """The learning phase, started on first use if one is configured. None if there isn't one."""
        st = eng.store
        phase = st.get("learning_phase")
        if phase is None:
            if self.c.learning_phase_budget_usd <= 0 or st.get("learning_phase_ended"):
                return None
            phase = {"start": now, "end": now + self.c.learning_phase_days * 86400, "budget": self.c.learning_phase_budget_usd}
            st.set("learning_phase", phase)
            st.log(f"Learning phase started: for the next {self.c.learning_phase_days} days my owner pays for my thinking, up to "
                   f"${phase['budget']:.0f}, so I can use my best model and learn as fast as possible. It doesn't come out of my balance.", ts=now)
        spent = -st.ledger_total_since("ai_sponsored", phase["start"])
        active = now < phase["end"] and spent < phase["budget"] - 0.5
        if not active and not st.get("learning_phase_ended"):
            st.set("learning_phase_ended", now)
            st.log(f"Learning phase over (${spent:.2f} of ${phase['budget']:.0f} used). From here I pay for my own thinking again.",
                   level="warning", ts=now)
        return {**phase, "spent": spent, "active": active}

    def pay(self, eng, now: int, cost: float, note: str) -> None:
        """Thinking comes out of Claude's balance, except during the owner-funded learning phase."""
        phase = self.learning(eng, now)
        if phase and phase["active"]:
            eng.store.add_ledger(now, "ai_sponsored", -cost, note)
        else:
            eng.charge(now, "ai_cost", cost, note)

    def tier(self, eng, now: int, price=None) -> dict:
        phase = self.learning(eng, now)
        if phase and phase["active"]:
            start, floor = eng.contributed, self.s.survival.floor_usd
            health = max(0.0, min(1.0, (eng.equity(price) - floor) / max(start - floor, 1e-9)))
            base = {"spent": phase["spent"], "budget": phase["budget"], "health": health, "pace": 0.0, "earned_smart": True,
                    "learning": True}
            if health < 0.25:
                return {**base, "name": "survival", "model": self.c.lean_model, "searches": 0, "heartbeat": False}
            return {**base, "name": "sharp", "model": self.c.smart_model, "searches": self.c.web_searches_per_wake, "heartbeat": True}
        allowance = self.career.allowance(eng.store, now)
        budget = min(allowance["amount"], self.c.monthly_budget_usd)
        spent = self.month_spend(eng, now)
        left = budget - spent
        start, floor = eng.contributed, self.s.survival.floor_usd
        health = max(0.0, min(1.0, (eng.equity(price) - floor) / max(start - floor, 1e-9)))
        d = datetime.fromtimestamp(now, timezone.utc)
        days_in_month = (datetime(d.year + d.month // 12, d.month % 12 + 1, 1, tzinfo=timezone.utc)
                         - datetime(d.year, d.month, 1, tzinfo=timezone.utc)).days
        elapsed = max((now - month_start(now)) / 86400, 1) / days_in_month
        pace = spent / (budget * elapsed) if budget else 99
        base = {"spent": spent, "budget": budget, "health": health, "pace": pace, "earned_smart": allowance["smart"]}
        if left < 0.5:
            return {**base, "name": "asleep", "model": None, "searches": 0, "heartbeat": False}
        if left < 2 or health < 0.25:
            return {**base, "name": "survival", "model": self.c.lean_model, "searches": 0, "heartbeat": False}
        if pace <= 1 and health >= 0.6 and allowance["smart"]:
            return {**base, "name": "sharp", "model": self.c.smart_model, "searches": self.c.web_searches_per_wake, "heartbeat": True}
        return {**base, "name": "lean", "model": self.c.lean_model, "searches": 0, "heartbeat": True}

    # Scanner
    def scan(self, eng, now: int) -> list[dict]:
        if now - self._scan[0] >= SCAN_EVERY or not self._scan[1]:
            try:
                feats = scanner.scan(eng.market, eng.g.min_volume_usd, list(eng.positions))
                self._scan = (now, feats)
            except Exception:
                self._scan = (now, self._scan[1])
        return self._scan[1]

    # When to wake
    def wake_reason(self, eng, candles, now: int, tier: dict) -> str | None:
        st = eng.store
        if now < (st.get("claude_retry_after") or 0):
            return None
        last = st.get("claude_last_wake_ts")
        if last is None:
            return "First look at the market"
        if now - last < self.c.min_minutes_between_wakes * 60:
            return None

        closes = [c.close for c in candles]
        if candles and candles[-1].ts != st.get("claude_last_candle_ts"):
            st.set("claude_last_candle_ts", candles[-1].ts)
            fast, slow = ema(closes, self.s.strategy.fast_ema), ema(closes, self.s.strategy.slow_ema)
            if None not in (fast[-2], slow[-2]):
                if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
                    return f"Trend signal: {eng.primary}'s 20 EMA just crossed above the 50 EMA on the 4h chart"
                if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
                    return f"Trend signal: {eng.primary}'s 20 EMA just crossed below the 50 EMA on the 4h chart"

        feats = {f["coin"]: f for f in self.scan(eng, now)}
        warned = st.get("claude_stop_warned") or {}
        for coin, pos in eng.positions.items():
            p, stop = eng.prices.get(coin), pos.get("stop")
            vol = (feats.get(coin) or {}).get("atr")
            if vol is None and coin == eng.primary and candles:
                vol = atr([c.high for c in candles], [c.low for c in candles], closes, self.s.strategy.atr_period)[-1]
            stamp = (feats.get(coin) or {}).get("candle_ts") or (candles[-1].ts if candles else 0)
            if p and stop and vol and abs(p - stop) <= vol and warned.get(coin) != stamp:
                warned[coin] = stamp
                st.set("claude_stop_warned", warned)
                return f"{coin} at {px(p)} is within one ATR of its stop at {px(stop)}"

        last_prices = st.get("claude_last_wake_prices") or {}
        for coin in [eng.primary, *eng.positions]:
            p, before = eng.prices.get(coin), last_prices.get(coin)
            if p and before and abs(p / before - 1) * 100 >= self.c.move_trigger_pct:
                return f"{coin} moved {(p / before - 1) * 100:+.1f}% since my last check"

        if eng.g.min_volume_usd:
            seen = st.get("claude_alerts_seen") or {}
            today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
            seen = {k: v for k, v in seen.items() if v == today}
            for f in feats.values():
                for kind, msg in scanner.alerts(f):
                    key = f"{f['coin']}:{kind}"
                    if key not in seen:
                        seen[key] = today
                        st.set("claude_alerts_seen", seen)
                        return f"Scanner: {msg}"

        if tier["heartbeat"]:
            next_check = st.get("claude_next_check_ts") or last + self.c.heartbeat_hours * 3600
            if now >= next_check:
                return "Scheduled check-in"
        return None

    # Main entry from the engine
    def decide(self, eng, candles, price: float, now: int):
        for r in learning.grade_calls(eng.store, eng.prices, now):
            eng.store.log(f"Shadow call marked: {r['coin']} {'long' if r['dir'] > 0 else 'short'} ({r['setup']}) from {px(r['entry'])} "
                          f"hit its {r['exit'] if r['exit'] != 'time' else 'time limit'} at {px(r['exit_price'])}, {r['r']:+.2f}R.", ts=now)
        key = self.api_key(eng)
        if not key:
            if not eng.store.get("claude_waiting_logged"):
                eng.store.set("claude_waiting_logged", True)
                eng.store.log("Waiting for an Anthropic API key before I can think. Add it in the dashboard settings.", level="warning", ts=now)
            return None
        self.career.roll_month(eng, self.rival, now)
        self.career.check_promotion(eng, self.rival, now)
        tier = self.tier(eng, now)
        if tier["name"] == "asleep":
            month = month_key(now)
            if eng.store.get("claude_asleep_logged") != month:
                eng.store.set("claude_asleep_logged", month)
                eng.store.log(f"Thinking budget for this month is used up (${tier['spent']:.2f} of ${tier['budget']:.2f}). "
                              "Sleeping until next month. My coded stops still protect my positions.", level="warning", ts=now)
            return None

        if self._review_due(eng, now) and tier["name"] in ("sharp", "lean"):
            self.review(eng, key, now, tier)
        learning.review_trades(self, eng, key, now, self.c.lean_model)

        reason = self.wake_reason(eng, candles, now, tier)
        if reason is None:
            return None
        previous = eng.store.get("claude_last_wake_ts"), eng.store.get("claude_last_wake_prices")
        eng.store.set("claude_last_wake_ts", now)
        eng.store.set("claude_last_wake_prices", {c: eng.prices[c] for c in [eng.primary, *eng.positions] if c in eng.prices})
        try:
            return self._think(eng, key, candles, now, tier, reason)
        except Exception as exc:
            # The check didn't happen, so put things back and retry the same wake in 15 minutes.
            eng.store.set("claude_last_wake_ts", previous[0])
            eng.store.set("claude_last_wake_prices", previous[1])
            eng.store.set("claude_retry_after", now + RETRY_AFTER_ERROR)
            eng.store.log(f"Couldn't reach Claude ({type(exc).__name__}: {exc}). Trying again in 15 minutes.", level="error", ts=now)
            return None

    # Talking to Claude
    def _call(self, key: str, model: str, system: str, user: str, tools: list) -> tuple[list, float, str]:
        """Runs one request, resuming if a server-side web search pauses the turn.
        Returns the final content blocks, the total cost and the model that answered."""
        client = self.client(key)
        messages = [{"role": "user", "content": user}]
        total = 0.0
        for _ in range(MAX_CONTINUATIONS + 1):
            params = dict(model=model, max_tokens=16000, system=system, messages=messages, tools=tools,
                          output_config={"effort": "medium"})
            if self._use_fallbacks:
                # If a safety classifier declines, Anthropic retries on a suitable model instead of failing.
                params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            try:
                resp = client.beta.messages.create(**params)
            except Exception as exc:
                if self._use_fallbacks and "fallback" in str(exc).lower():
                    self._use_fallbacks = False
                    continue
                raise
            answered_by = getattr(resp, "model", None) or model
            total += usage_cost(resp.usage, answered_by)
            if resp.stop_reason == "pause_turn":
                messages = [{"role": "user", "content": user}, {"role": "assistant", "content": resp.content}]
                continue
            if resp.stop_reason == "refusal":
                raise RuntimeError("Claude declined to answer this request")
            return resp.content, total, answered_by
        raise RuntimeError("Claude kept pausing without finishing")

    def system_prompt(self, eng) -> str:
        g = eng.g
        stats = knowledge("setup_stats.md")
        practice = eng.store.get("practice_lessons") or ""
        return SYSTEM_PROMPT.format(
            max_positions=g.max_positions,
            style=AGGRESSIVE_STYLE if g.style == "full send" else PATIENT_STYLE,
            rules=rules_text(g, self.s.survival.floor_usd), min_check=f"{self.c.min_check_hours:g}",
            playbook=knowledge("playbook.md"),
            stats=f"\n# Backtested setup statistics\n{stats}\n" if stats else "",
            practice=f"\n# Lessons from your practice run on historical charts\n{practice}\n" if practice else "")

    def _think(self, eng, key, candles, now, tier, reason):
        model = tier["model"]
        tools = [DECISION_TOOL]
        if tier["searches"]:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": tier["searches"]})
        user = self.build_context(eng, candles, now, tier, reason)
        content, cost, answered_by = self._call(key, model, self.system_prompt(eng), user, tools)
        self.pay(eng, now, cost, f"{answered_by}: {reason}")

        call = next((b for b in content if getattr(b, "type", None) == "tool_use" and b.name == "submit_decisions"), None)
        if call is None:
            eng.store.add_decision(now, "decision", answered_by, reason, None, None, "No decision returned.", None, cost, eng.last_price)
            eng.store.log(f"Claude ({answered_by}, ${cost:.3f}) didn't return a decision, so holding.", level="warning", ts=now)
            return None
        d = call.input if isinstance(call.input, dict) else json.loads(call.input)
        hours = min(max(float(d.get("next_check_hours") or self.c.heartbeat_hours), self.c.min_check_hours), 48)
        eng.store.set("claude_next_check_ts", now + int(hours * 3600))

        decisions, labels = [], []
        why = f"Claude: {d.get('reasoning', '')}"
        equity = eng.equity()
        for a in d.get("actions") or []:
            coin = (a.get("coin") or eng.primary).upper().strip()
            act, stop = a.get("action"), a.get("stop_price")
            if act in ("long", "short"):
                pct = a.get("position_pct") or 0
                decisions.append(Decision("buy" if act == "long" else "short", why, stop, size_usd=equity * pct / 100,
                                          coin=coin, setup=a.get("setup")))
                labels.append(f"{act.upper()} {coin} {pct:g}%")
            elif act == "close":
                frac = (a.get("close_pct") or 100) / 100
                decisions.append(Decision("sell", why, sell_fraction=min(max(frac, 0.0), 1.0), coin=coin))
                labels.append(f"CLOSE {coin}" + (f" {frac:.0%}" if frac < 1 else ""))
            elif act == "set_stop" and stop:
                decisions.append(Decision("hold", "Claude moved the stop", stop, coin=coin))
                labels.append(f"STOP {coin} {px(stop)}")
        summary = ", ".join(labels) or "HOLD"
        called = learning.add_calls(eng.store, d.get("calls") or [], eng.prices, now)
        if called:
            summary += f" (shadow calls: {', '.join(called)})"
        eng.store.add_decision(now, "decision", answered_by, reason, summary, d.get("confidence"),
                               d.get("reasoning", ""), d.get("journal"), cost, eng.last_price)
        eng.store.log(f"Claude ({answered_by.replace('claude-', '')}, {tier['name']} mode, cost ${cost:.3f}) woke because: {reason}. "
                      f"Decision: {summary}. {d.get('reasoning', '')}", level="thought", ts=now)
        return decisions

    # What Claude sees
    def build_context(self, eng, candles, now, tier, reason) -> str:
        st, s = eng.store, self.s
        equity = eng.equity()
        lines = [f"## Why you were woken\n{reason}\n", f"Time: {fmt_time(now)}\n"]

        burn = eng.daily_burn()
        runway = (equity - s.survival.floor_usd) / burn if burn > 0 else float("inf")
        start_price = st.get("start_price")
        lines.append("## Your state")
        lines.append(f"- Equity {equity:.2f} USDC (put in so far {eng.contributed:.2f}, born {fmt_time(st.get('started_at'))}). "
                     f"Survival floor {s.survival.floor_usd:.2f}. Health {tier['health']:.0%}.")
        lines.append(f"- Cash {eng.cash:.2f} USDC. Exposure {eng.exposure():.2f} USDC of a {equity * eng.g.max_leverage:.2f} limit.")
        if not eng.positions:
            lines.append("- No open positions.")
        for coin, p in eng.positions.items():
            price = eng.prices.get(coin)
            side = "LONG" if p["qty"] > 0 else "SHORT"
            entry = abs(p["cost_basis"] / p["qty"])
            unreal = p["qty"] * price - p["cost_basis"] if price else 0
            lines.append(f"- {side} {coin}: {abs(p['qty']):.6g} ({abs(p['qty']) * (price or 0):.2f} USDC), entry {px(entry)}, now {px(price or 0)}, "
                         f"unrealised {unreal:+.2f}, stop {px(p['stop']) if p.get('stop') else 'none'}, setup {p.get('setup') or '?'}.")
        funding = -st.ledger_total("funding")
        if funding:
            lines.append(f"- Funding {'paid' if funding >= 0 else 'received'} so far: {abs(funding):.3f} USDC.")
        if tier.get("learning"):
            lines.append("- Learning phase: your owner is paying for your thinking for now, so use it to learn fast: make every shadow call count.")
        lines.append(f"- Thinking budget: spent ${tier['spent']:.2f} of ${tier['budget']:.2f} {'in the learning phase' if tier.get('learning') else 'this month'}. Mode: {tier['name']}. "
                     f"Average running cost ${burn:.3f}/day, about {runway:.0f} days to the floor if you make nothing.")
        lines.append(self._pace_text(eng, now, tier))
        if start_price and eng.last_price:
            lines.append(f"- Since you were born ETH is {(eng.last_price / start_price - 1) * 100:+.1f}% and you are "
                         f"{(equity / eng.contributed - 1) * 100:+.1f}%.")
        limit = eng.g.max_trades_per_day
        lines.append(f"- Trades today: {eng.trades_today(now)}" + (f" of {limit} allowed.\n" if limit else " (no limit).\n"))

        lines.append("## Your career")
        lines.append(self.career.context(st, self.rival, now) + "\n")

        feats = self.scan(eng, now)
        if feats and eng.g.min_volume_usd:
            lines.append("## Scanner: most interesting liquid coins right now (4h candles)")
            lines.append(scanner.table(feats, list(eng.positions)) + "\n")

        lines.append(f"## {eng.primary} in detail")
        lines.append(self._market_block(eng, candles))
        for coin in eng.positions:
            if coin != eng.primary:
                lines.append(self._coin_block(eng, coin))

        if self.rival is not None and self.rival.last_price:
            r = self.rival.summary()
            pos = "holding ETH" if r["positions"] else "in cash"
            lines.append(f"## The maths bot (your rival)\nEquity {r['equity']:.2f} from {r['starting_balance']:.0f}, currently {pos}.\n")

        trades = st.trades(12)
        if trades:
            lines.append("## Your recent trades (newest first)")
            for t in trades:
                pnl = f", P&L {t['pnl']:+.2f}" if t["pnl"] is not None else ""
                lines.append(f"- {fmt_time(t['ts'])} {t['side'].upper()} {t['qty']:.6g} {t.get('coin') or 'ETH'} @ {px(t['price'])}{pnl}")
            lines.append("")
        decisions = st.decisions(6, kind="decision")
        if decisions:
            lines.append("## Your recent decisions (newest first)")
            for d in decisions:
                lines.append(f"- {fmt_time(d['ts'])}: {d['action'] or 'none'} ({d['confidence'] or '?'}). "
                             f"{d['reasoning']} Journal: {d['journal'] or ''}")
            lines.append("")
        lines.append("## Your scorecard")
        lines.append(learning.scorecard(st) + "\n")
        lines.append("## Your lessons learned")
        lines.append(st.get("lessons") or "None yet from live trading. Lean on your playbook, the setup stats and your practice lessons.")
        lines.append("\nDecide now and call submit_decisions.")
        return "\n".join(lines)

    def _pace_text(self, eng, now: int, tier: dict) -> str:
        """How fast thinking money is going, so Claude can judge how often to look."""
        st = eng.store
        day = -(st.ledger_total_since("ai_cost", now - 86400) + st.ledger_total_since("ai_sponsored", now - 86400))
        checks = st.decisions(200, kind="decision", since=now - 86400)
        avg = sum(d["cost"] or 0 for d in checks) / len(checks) if checks else None
        left = max(tier["budget"] - tier["spent"], 0)
        if tier.get("learning"):
            phase = self.learning(eng, now)
            ends = phase["end"]
        else:
            d = datetime.fromtimestamp(now, timezone.utc)
            ends = int(datetime(d.year + d.month // 12, d.month % 12 + 1, 1, tzinfo=timezone.utc).timestamp())
        days_left = max((ends - now) / 86400, 0.01)
        text = (f"- Pace: {len(checks)} checks in the last 24 hours" + (f" at about ${avg:.3f} each" if avg else "")
                + f", ${day:.2f} in total. ${left:.2f} left for the next {days_left:.1f} days, about ${left / days_left:.2f} a day.")
        if day > 0 and left / day < days_left:
            text += f" At today's pace it runs out in {left / day:.1f} days, after which you drop to the cheaper model or sleep."
        return text

    def _market_block(self, eng, candles) -> str:
        s, m = self.s.strategy, eng.market
        price = eng.last_price
        out = []
        closes = [c.close for c in candles]
        if candles and price:
            f, sl = ema(closes, s.fast_ema)[-1], ema(closes, s.slow_ema)[-1]
            vol = atr([c.high for c in candles], [c.low for c in candles], closes, s.atr_period)[-1]
            r = rsi(closes)[-1]
            ch24 = (price / closes[-7] - 1) * 100 if len(closes) >= 7 else 0
            ch7d = (price / closes[-43] - 1) * 100 if len(closes) >= 43 else 0
            out.append(f"{eng.primary} {px(price)}. 24h {ch24:+.1f}%, 7d {ch7d:+.1f}%.")
            out.append(f"4h chart: 20 EMA {px(f)}, 50 EMA {px(sl)}, ATR {px(vol)} ({vol / price * 100:.1f}%), RSI {r:.0f}.")
            out.append("Last 20 4h candles (UTC open time, open, high, low, close):")
            for c in candles[-20:]:
                out.append(f"{datetime.fromtimestamp(c.ts, timezone.utc).strftime('%d %b %H:%M')} {c.open:.2f} {c.high:.2f} {c.low:.2f} {c.close:.2f}")
        for fn in (self._perp, self._hourly, self._daily, self._btc, self._sentiment):
            try:
                text = fn(m)
                if text:
                    out.append(text)
            except Exception:
                pass
        return "\n".join(out) + "\n"

    def _coin_block(self, eng, coin: str) -> str:
        try:
            c4 = eng.market.candles(240, coin=coin, count=60)
            closes = [c.close for c in c4]
            f, sl = ema(closes, 20)[-1], ema(closes, 50)[-1]
            rows = " | ".join(f"{datetime.fromtimestamp(c.ts, timezone.utc).strftime('%d %H:%M')} {c.low:.6g}-{c.high:.6g} close {c.close:.6g}" for c in c4[-12:])
            perp = eng.market.perp_context(coin) or {}
            out = (f"## {coin} (held)\n4h: 20 EMA {px(f)}, 50 EMA {px(sl)}, RSI {rsi(closes)[-1]:.0f}, "
                   f"funding {perp.get('funding', 0) * 24 * 365 * 100:+.0f}% a year. Last 12 4h candles: {rows}\n")
            try:
                h1 = eng.market.candles(60, coin=coin, count=12)
                out += "Last 12 1h candles: " + " | ".join(
                    f"{datetime.fromtimestamp(c.ts, timezone.utc).strftime('%H:%M')} {c.low:.6g}-{c.high:.6g} close {c.close:.6g}" for c in h1) + "\n"
            except Exception:
                pass
            return out
        except Exception:
            return f"## {coin} (held)\nNo detail available right now.\n"

    def _perp(self, m) -> str | None:
        p = m.perp_context()
        if not p:
            return None
        yearly = p["funding"] * 24 * 365 * 100
        who = "longs pay shorts" if p["funding"] >= 0 else "shorts pay longs"
        return (f"Hyperliquid ETH perp: funding {p['funding'] * 100:.4f}% per hour (about {yearly:+.0f}% a year, {who}), "
                f"open interest {p['open_interest']:,.0f}, 24h volume ${p['volume_24h'] / 1e6:,.0f}M.")

    def _hourly(self, m) -> str:
        hourly = m.candles(60)
        closes = [c.close for c in hourly]
        rows = " | ".join(f"{datetime.fromtimestamp(c.ts, timezone.utc).strftime('%H:%M')} {c.low:.0f}-{c.high:.0f} close {c.close:.0f}" for c in hourly[-24:])
        return f"1h chart: 20 EMA {ema(closes, 20)[-1]:,.2f}, RSI {rsi(closes)[-1]:.0f}. Last 24 hours (UTC open time, low-high, close): {rows}"

    def _daily(self, m) -> str:
        daily = m.candles(1440)
        closes = [c.close for c in daily]
        f, sl = ema(closes, 20)[-1], ema(closes, 50)[-1]
        recent = ", ".join(f"{c:.0f}" for c in closes[-30:])
        hi, lo = max(c.high for c in daily[-90:]), min(c.low for c in daily[-90:])
        return (f"Daily chart: 20 EMA {f:,.2f}, 50 EMA {sl:,.2f}, RSI {rsi(closes)[-1]:.0f}. 90 day range {lo:,.0f} to {hi:,.0f}.\n"
                f"Last 30 daily closes (oldest first): {recent}")

    def _btc(self, m) -> str:
        btc = m.candles(240, coin="BTC", pair="XBTUSDT")
        closes = [c.close for c in btc]
        f, sl = ema(closes, 20)[-1], ema(closes, 50)[-1]
        ch24 = (closes[-1] / closes[-7] - 1) * 100
        ch7d = (closes[-1] / closes[-43] - 1) * 100
        return (f"BTC {closes[-1]:,.0f}. 24h {ch24:+.1f}%, 7d {ch7d:+.1f}%. 4h trend {'up' if f > sl else 'down'} "
                f"(20 EMA {f:,.0f} vs 50 EMA {sl:,.0f}).")

    def _sentiment(self, m) -> str | None:
        fg = m.fear_greed()
        return f"Crypto Fear & Greed Index: {fg[0]} ({fg[1]})." if fg else None

    # Weekly review: this is where Claude learns
    def _review_due(self, eng, now: int) -> bool:
        last = eng.store.get("claude_last_review_ts") or eng.store.get("started_at")
        if now - last < self.c.review_every_days * 86400:
            return False
        return len(eng.store.decisions(100, kind="decision", since=last)) >= 3

    def review(self, eng, key: str, now: int, tier: dict) -> None:
        st = eng.store
        since = st.get("claude_last_review_ts") or st.get("started_at")
        model = self.c.review_model if tier["name"] == "sharp" else self.c.lean_model
        lines = [f"## Period\n{fmt_time(since)} to {fmt_time(now)}\n", "## Your current lessons", st.get("lessons") or "None yet.", ""]
        eq_then = next(iter(st.equity_history(since)), None)
        price = eng.last_price
        if eq_then and price:
            lines.append(f"## Results\nYour equity went from {eq_then['equity']:.2f} to {eng.equity():.2f}. "
                         f"ETH went from {eq_then['price']:,.2f} to {price:,.2f} ({(price / eq_then['price'] - 1) * 100:+.1f}%).")
        if self.rival is not None and self.rival.last_price:
            lines.append(f"The maths bot is at {self.rival.summary()['equity']:.2f}.")
        lines.append(f"You spent ${-st.ledger_total_since('ai_cost', since):.2f} on thinking in this period.")
        lines.append(self.career.context(st, self.rival, now) + "\n")
        lines.append("## Your scorecard (trades by setup, shadow calls, lessons from each closed trade)")
        lines.append(learning.scorecard(st) + "\n")
        lines.append("## Your decisions (oldest first)")
        for d in reversed(st.decisions(100, kind="decision", since=since)):
            lines.append(f"- {fmt_time(d['ts'])}, woken because: {d['wake_reason']}. {d['action'] or 'none'} "
                         f"({d['confidence'] or '?'}). {d['reasoning']} Journal: {d['journal'] or ''}")
        lines.append("\n## Trades (oldest first)")
        for t in reversed([t for t in st.trades(300) if t["ts"] >= since]):
            pnl = f", P&L {t['pnl']:+.2f}" if t["pnl"] is not None else ""
            lines.append(f"- {fmt_time(t['ts'])} {t['side'].upper()} {t.get('coin') or 'ETH'} @ {px(t['price'])}{pnl}. {t['reason'][:120]}")
        lines.append("\nReview your week and call submit_review.")
        try:
            content, cost, answered_by = self._call(key, model, REVIEW_PROMPT, "\n".join(lines), [REVIEW_TOOL])
        except Exception as exc:
            st.set("claude_last_review_ts", now - (self.c.review_every_days - 1) * 86400)  # try again tomorrow
            st.log(f"Weekly review failed ({exc}). Will try again tomorrow.", level="error", ts=now)
            return
        self.pay(eng, now, cost, f"{answered_by}: weekly review")
        st.set("claude_last_review_ts", now)
        call = next((b for b in content if getattr(b, "type", None) == "tool_use" and b.name == "submit_review"), None)
        if call is None:
            st.log(f"Weekly review (${cost:.3f}) came back without updated lessons.", level="warning", ts=now)
            return
        r = call.input if isinstance(call.input, dict) else json.loads(call.input)
        st.set("lessons", r["lessons"])
        st.set("lessons_updated", now)
        st.add_decision(now, "review", answered_by, "Weekly review", None, None, r["summary"], r["lessons"], cost, price)
        st.log(f"Weekly review done (cost ${cost:.3f}). {r['summary']}", level="thought", ts=now)

    def on_close(self, eng, coin, pos, pnl, fraction, now, exit_price=None, reason="") -> None:
        learning.record_close(eng.store, coin, pos, pnl, fraction, now, exit_price or eng.prices.get(coin, 0), reason)

    # Dashboard
    def extra_summary(self, eng) -> dict:
        now = int(self.clock())
        tier = self.tier(eng, now) if eng.prices else None
        last = eng.store.decisions(1, kind="decision")
        scan = self._scan[1][:8]
        return {"brain": {
            "key_set": bool(self.api_key(eng)),
            "venue": (eng.store.get("venue") or {"name": "paper"})["name"],
            "venue_account": (eng.store.get("venue") or {}).get("account"),
            "allow_mainnet": self.c.allow_mainnet,
            "style": eng.g.style,
            "key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "tier": tier["name"] if tier else None,
            "model": tier["model"] if tier else None,
            "spent_this_month": tier["spent"] if tier else self.month_spend(eng, now),
            "learning": self.learning(eng, now),
            "sponsored": -eng.store.ledger_total("ai_sponsored"),
            "budget": tier["budget"] if tier else self.c.monthly_budget_usd,
            "cap": self.c.monthly_budget_usd,
            "last_wake": eng.store.get("claude_last_wake_ts"),
            "next_check": eng.store.get("claude_next_check_ts"),
            "lessons": eng.store.get("lessons"),
            "lessons_updated": eng.store.get("lessons_updated"),
            "practice": eng.store.get("practice_status"),
            "practice_lessons": eng.store.get("practice_lessons"),
            "last_decision": last[0] if last else None,
            "decisions": eng.store.decisions(20),
            "career": self.career.summary(eng.store, now),
            "scorecard": learning.stats(eng.store),
            "scanner": [{k: f[k] for k in ("coin", "price", "ch_24h", "trend", "breakout", "breakdown", "funding_apr", "volume_m")} for f in scan],
        }}
