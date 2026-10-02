"""Claude as the trader.

Cheap code watches the market every minute for free. Claude is only woken when
something is worth a decision: a trend signal, a big move, price closing in on the
stop, or a scheduled check-in. Every thought costs money, which comes out of the
bot's own balance, so Claude has to earn its intelligence:

  * sharp:    healthy and on budget. Smartest model, can search the news.
  * lean:     money or budget getting tight. Cheaper model, no news searches.
  * survival: close to the floor or nearly out of budget. Cheap model, only wakes for real events.
  * asleep:   monthly budget used up. Only the coded stops protect it until next month.

Claude learns by keeping a journal and, once a week, reviewing its own decisions and
rewriting a short "lessons learned" note that it reads before every decision.
"""

import json
import os
import time
from datetime import datetime, timezone

from .config import Settings
from .indicators import atr, ema, rsi
from .strategy import Decision

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

SYSTEM_PROMPT = """You are the trading mind of Survival Bot, an autonomous crypto trader with its own small USDC account. You trade the ETH perpetual on Hyperliquid against USDC, with no leverage: you can be in cash, long ETH (profit when it rises) or short ETH (profit when it falls), never more exposure than your equity.

Your situation is unusual. You pay for your own existence. Hosting and every time you are woken up to think are paid out of your own balance. If your equity falls below the survival floor you die: everything is sold and you never trade again.

{style}

Hard limits enforced in code (you cannot override them, so plan within them):
{rules}

Each time you are woken you will get your current state, the market data, your recent decisions and your lessons learned. Think it through, then call the submit_decision tool exactly once with your decision. If you have web search available, use it when news could change the decision. It costs a cent or two each time.

Actions:
- long: go long, or add to your long. Give position_pct (percent of equity for this trade) and stop_price below the price.
- short: go short, or add to your short. Give position_pct and stop_price above the price.
  Choosing the opposite side of your current position closes it first and then opens the new one.
- close: close close_pct percent of your position (100 to exit completely).
- set_stop: keep the position and move the stop to stop_price.
- hold: do nothing.

Perps charge or pay funding every hour: when the rate is positive, longs pay shorts, and when it is negative, shorts pay longs. It comes out of your balance like any other cost, and a very high or very negative rate also tells you how crowded each side is.

Set next_check_hours to when you next want to look if nothing else happens ({min_check} to 48). You are also woken automatically on big moves, trend signals and when price nears your stop."""

PATIENT_STYLE = """Your owner wants you to stay alive first and grow second, and to get better over time. Trade like a disciplined professional:
- Capital preservation beats activity. Holding cash is a position. Most of the time the right answer is to do nothing.
- Only take trades with a clear edge and at least 2 to 1 reward to risk. Know where you are wrong before you enter.
- Trade with the dominant trend and the wider market (BTC, sentiment, news). Shorting is allowed only if your rules say so, and only in a clear downtrend.
- Fees, slippage and your own thinking costs eat small gains. Avoid churn.
- Protect winners by raising the stop. Never hope a loser comes back.
- Be honest in your journal, so you can learn from it later."""

AGGRESSIVE_STYLE = """Your owner wants you to be aggressive. Growth is the goal and sitting in cash waiting for perfection is not. The guardrails are deliberately loose: only the survival floor and your thinking budget really hold you back. Trade like a bold, skilled discretionary trader:
- Hunt for opportunities in both directions. Momentum, breakouts, range trades, dip buys, shorting failed rallies and breakdowns, and short swings on the 1h and 4h charts are all fair game. Don't sit in cash just because the trend is down: that is a short opportunity. A decent edge is enough, you don't need a perfect setup.
- Size up when you have conviction. Add to winners while the move is working. Take partial profits into strength and let the rest run.
- Cut losers fast and re-enter when the setup returns. Being wrong small is fine, being frozen is not.
- Still respect the maths: fees and slippage are about 0.3% per round trip and every check costs you, so a trade needs room to move.
- Remember that dying ends everything. Aggressive does not mean reckless near the floor: as your health drops, size down.
- Be honest in your journal about what you expected and why, so your weekly review can sharpen you."""

REVIEW_PROMPT = """You are the trading mind of Survival Bot doing your weekly self review. You pay for every thought out of your own small balance, and you die if equity falls below the survival floor, so the point of this review is to make your future decisions better and cheaper.

You will get your current lessons, every decision you made since the last review with the reasoning you gave at the time, the trades and their results, and how you did against simply holding ETH and against the simple maths bot running alongside you.

Be a tough, honest coach. What worked? What was a mistake, and was it bad luck or bad judgement? Were you woken too often or not enough? Then rewrite your lessons learned as short, specific, actionable rules (at most 12 bullet points, under 300 words). Keep lessons that still hold, drop ones that proved wrong, add new ones. Call the submit_review tool once."""

DECISION_TOOL = {
    "name": "submit_decision",
    "description": "Submit your trading decision for this check. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["long", "short", "close", "set_stop", "hold"]},
            "position_pct": {"type": ["number", "null"], "description": "For long or short: percent of equity for this trade."},
            "close_pct": {"type": ["number", "null"], "description": "For close: percent of the position to close, 100 to exit completely."},
            "stop_price": {"type": ["number", "null"], "description": "For long, short or set_stop: the stop price for the whole position."},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "reasoning": {"type": "string", "description": "Plain English explanation for your owner, 2 to 5 sentences."},
            "journal": {"type": "string", "description": "A short note to your future self: what you expect to happen and what would prove you wrong."},
            "next_check_hours": {"type": "number", "description": "When to check in next if nothing else happens, in hours (max 48)."},
        },
        "required": ["action", "position_pct", "close_pct", "stop_price", "confidence", "reasoning", "journal", "next_check_hours"],
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
        f"- Trades are trimmed so that hitting the stop loses at most {g.max_risk_per_trade * 100:g}% of equity across the whole position.",
        "- You can go short." if g.allow_short else "- No shorting: long or cash only.",
        f"- No leverage: total exposure is capped at {g.max_leverage:g}x your equity.",
        f"- At most {g.max_trades_per_day} trades per day." if g.max_trades_per_day else "- No limit on trades per day.",
        "- You can add to a position you already hold." if g.allow_adding else "- No adding to an open position.",
        "- Stops can only be tightened." if g.stops_only_up else "- You can move your stop in either direction.",
        "- A coded stop sells automatically within a minute if price touches it, even while you sleep.",
        (f"- If you lose {g.daily_loss_limit_pct:g}% in a UTC day, everything is sold and you sit out until tomorrow."
         if g.daily_loss_limit_pct else "- No daily loss limit."),
        f"- Below {floor:.0f} USDC equity you die.",
    ]
    return "\n".join(rules)


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


class ClaudeBrain:
    name = "claude"
    label = "Claude"

    def __init__(self, settings: Settings, client_factory=None, clock=time.time):
        self.s = settings
        self.c = settings.claude
        self.clock = clock
        self.rival = None  # the maths bot's engine, for comparison in reviews
        self._client_factory = client_factory
        self._client = None
        self._client_key = None
        self._use_fallbacks = True

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

    def tier(self, eng, now: int, price: float) -> dict:
        spent = self.month_spend(eng, now)
        left = self.c.monthly_budget_usd - spent
        start, floor = self.s.bot.starting_balance, self.s.survival.floor_usd
        health = max(0.0, min(1.0, (eng.equity(price) - floor) / (start - floor)))
        d = datetime.fromtimestamp(now, timezone.utc)
        days_in_month = (datetime(d.year + d.month // 12, d.month % 12 + 1, 1, tzinfo=timezone.utc)
                         - datetime(d.year, d.month, 1, tzinfo=timezone.utc)).days
        elapsed = max((now - month_start(now)) / 86400, 1) / days_in_month
        pace = spent / (self.c.monthly_budget_usd * elapsed)
        base = {"spent": spent, "budget": self.c.monthly_budget_usd, "health": health, "pace": pace}
        if left < 0.5:
            return {**base, "name": "asleep", "model": None, "searches": 0, "heartbeat": False}
        if left < 2 or health < 0.25:
            return {**base, "name": "survival", "model": self.c.lean_model, "searches": 0, "heartbeat": False}
        if pace <= 1 and health >= 0.6:
            return {**base, "name": "sharp", "model": self.c.smart_model, "searches": self.c.web_searches_per_wake, "heartbeat": True}
        return {**base, "name": "lean", "model": self.c.lean_model, "searches": 0, "heartbeat": True}

    # When to wake
    def wake_reason(self, eng, candles, price: float, now: int, tier: dict) -> str | None:
        st = eng.store
        if now < (st.get("claude_retry_after") or 0):
            return None
        last = st.get("claude_last_wake_ts")
        if last is None:
            return "First look at the market"
        if now - last < self.c.min_minutes_between_wakes * 60:
            return None

        closes = [c.close for c in candles]
        new_candle = candles and candles[-1].ts != st.get("claude_last_candle_ts")
        if new_candle:
            st.set("claude_last_candle_ts", candles[-1].ts)
            fast, slow = ema(closes, self.s.strategy.fast_ema), ema(closes, self.s.strategy.slow_ema)
            if None not in (fast[-2], slow[-2]):
                if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
                    return "Trend signal: the 20 EMA just crossed above the 50 EMA on the 4h chart"
                if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
                    return "Trend signal: the 20 EMA just crossed below the 50 EMA on the 4h chart"

        stop = st.get("stop")
        if eng.qty != 0 and stop and candles:
            vol = atr([c.high for c in candles], [c.low for c in candles], closes, self.s.strategy.atr_period)[-1]
            if vol and abs(price - stop) <= vol and st.get("claude_stop_warn_candle") != candles[-1].ts:
                st.set("claude_stop_warn_candle", candles[-1].ts)
                return f"Price {price:,.2f} is within one ATR of my stop at {stop:,.2f}"

        last_price = st.get("claude_last_wake_price")
        if last_price and abs(price / last_price - 1) * 100 >= self.c.move_trigger_pct:
            return f"Price moved {(price / last_price - 1) * 100:+.1f}% since my last check"

        if tier["heartbeat"]:
            next_check = st.get("claude_next_check_ts") or last + self.c.heartbeat_hours * 3600
            if now >= next_check:
                return "Scheduled check-in"
        return None

    # Main entry from the engine
    def decide(self, eng, candles, price: float, now: int) -> Decision | None:
        key = self.api_key(eng)
        if not key:
            if not eng.store.get("claude_waiting_logged"):
                eng.store.set("claude_waiting_logged", True)
                eng.store.log("Waiting for an Anthropic API key before I can think. Add it in the dashboard settings.", level="warning", ts=now)
            return None
        tier = self.tier(eng, now, price)
        if tier["name"] == "asleep":
            month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
            if eng.store.get("claude_asleep_logged") != month:
                eng.store.set("claude_asleep_logged", month)
                eng.store.log(f"Thinking budget for this month is used up (${tier['spent']:.2f}). Sleeping until next month. "
                              "My coded stops still protect any open position.", level="warning", ts=now)
            return None

        if self._review_due(eng, now) and tier["name"] in ("sharp", "lean"):
            self.review(eng, key, now, price, tier)

        reason = self.wake_reason(eng, candles, price, now, tier)
        if reason is None:
            return None
        previous = eng.store.get("claude_last_wake_ts"), eng.store.get("claude_last_wake_price")
        eng.store.set("claude_last_wake_ts", now)
        eng.store.set("claude_last_wake_price", price)
        try:
            return self._think(eng, key, candles, price, now, tier, reason)
        except Exception as exc:
            # The check didn't happen, so put things back and retry the same wake in 15 minutes.
            eng.store.set("claude_last_wake_ts", previous[0])
            eng.store.set("claude_last_wake_price", previous[1])
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

    def _think(self, eng, key, candles, price, now, tier, reason) -> Decision | None:
        model = tier["model"]
        tools = [DECISION_TOOL]
        if tier["searches"]:
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": tier["searches"]})
        g = eng.g
        system = SYSTEM_PROMPT.format(
            style=AGGRESSIVE_STYLE if g.style == "full send" else PATIENT_STYLE,
            rules=rules_text(g, self.s.survival.floor_usd), min_check=f"{self.c.min_check_hours:g}")
        user = self.build_context(eng, candles, price, now, tier, reason)
        content, cost, answered_by = self._call(key, model, system, user, tools)
        eng.charge(now, "ai_cost", cost, f"{answered_by}: {reason}")

        call = next((b for b in content if getattr(b, "type", None) == "tool_use" and b.name == "submit_decision"), None)
        if call is None:
            eng.store.add_decision(now, "decision", answered_by, reason, None, None, "No decision returned.", None, cost, price)
            eng.store.log(f"Claude ({answered_by}, ${cost:.3f}) didn't return a decision, so holding.", level="warning", ts=now)
            return None
        d = call.input if isinstance(call.input, dict) else json.loads(call.input)
        action = d.get("action", "hold")
        hours = min(max(float(d.get("next_check_hours") or self.c.heartbeat_hours), self.c.min_check_hours), 48)
        eng.store.set("claude_next_check_ts", now + int(hours * 3600))
        eng.store.add_decision(now, "decision", answered_by, reason, action, d.get("confidence"),
                               d.get("reasoning", ""), d.get("journal"), cost, price)
        eng.store.log(f"Claude ({answered_by.replace('claude-', '')}, {tier['name']} mode, cost ${cost:.3f}) woke because: {reason}. "
                      f"Decision: {action.upper().replace('_', ' ')}. {d.get('reasoning', '')}", level="thought", ts=now)

        stop = d.get("stop_price")
        why = f"Claude: {d.get('reasoning', '')}"
        if action in ("long", "buy", "short"):
            pct = d.get("position_pct") or 0
            return Decision("short" if action == "short" else "buy", why, stop, size_usd=eng.equity(price) * pct / 100)
        if action in ("close", "sell"):
            frac = (d.get("close_pct") or d.get("sell_pct") or 100) / 100
            return Decision("sell", why, sell_fraction=min(max(frac, 0.0), 1.0))
        if action in ("set_stop", "raise_stop") and stop:
            return Decision("hold", "Claude moved the stop", stop)
        return None

    # What Claude sees
    def build_context(self, eng, candles, price, now, tier, reason) -> str:
        st, s = eng.store, self.s
        equity = eng.equity(price)
        lines = [f"## Why you were woken\n{reason}\n", f"Time: {fmt_time(now)}\n"]

        burn = eng.daily_burn()
        runway = (equity - s.survival.floor_usd) / burn if burn > 0 else float("inf")
        start_price = st.get("start_price")
        lines.append("## Your state")
        lines.append(f"- Equity {equity:.2f} USDC (started with {s.bot.starting_balance:.2f}, born {fmt_time(st.get('started_at'))}). "
                     f"Survival floor {s.survival.floor_usd:.2f}. Health {tier['health']:.0%}.")
        if eng.qty == 0:
            lines.append("- No open position (all cash).")
        else:
            basis = st.get("cost_basis")
            side = "LONG" if eng.qty > 0 else "SHORT"
            entry = abs(basis / eng.qty)
            lines.append(f"- Open position: {side} {abs(eng.qty):.5f} ETH (about {abs(eng.qty) * price:.2f} USDC of exposure), "
                         f"average entry {entry:,.2f}, unrealised {eng.qty * price - basis:+.2f}, stop at {st.get('stop'):,.2f}.")
            funding = -st.ledger_total("funding")
            lines.append(f"- Funding {'paid' if funding >= 0 else 'received'} so far: {abs(funding):.3f} USDC.")
        lines.append(f"- Thinking budget: spent ${tier['spent']:.2f} of ${tier['budget']:.2f} this month. Mode: {tier['name']}. "
                     f"Average running cost ${burn:.3f}/day, about {runway:.0f} days to the floor if you make nothing.")
        if start_price:
            lines.append(f"- Since you were born ETH is {(price / start_price - 1) * 100:+.1f}% and you are "
                         f"{(equity / s.bot.starting_balance - 1) * 100:+.1f}%.")
        limit = eng.g.max_trades_per_day
        lines.append(f"- Trades today: {eng.trades_today(now)}" + (f" of {limit} allowed.\n" if limit else " (no limit).\n"))

        lines.append("## Market")
        lines.append(self._market_block(eng, candles, price))

        if self.rival is not None and self.rival.last_price:
            r = self.rival.summary()
            pos = "holding ETH" if r["position"] else "in cash"
            lines.append(f"## The maths bot (your rival, same $100 start)\nEquity {r['equity']:.2f}, currently {pos}.\n")

        trades = st.trades(10)
        if trades:
            lines.append("## Your recent trades (newest first)")
            for t in trades:
                pnl = f", P&L {t['pnl']:+.2f}" if t["pnl"] is not None else ""
                lines.append(f"- {fmt_time(t['ts'])} {t['side'].upper()} {t['qty']:.5f} @ {t['price']:,.2f}{pnl}")
            lines.append("")
        decisions = st.decisions(6, kind="decision")
        if decisions:
            lines.append("## Your recent decisions (newest first)")
            for d in decisions:
                at = f" at {d['price']:,.2f}" if d["price"] else ""
                lines.append(f"- {fmt_time(d['ts'])}{at}: {(d['action'] or 'none').upper()} ({d['confidence'] or '?'}). "
                             f"{d['reasoning']} Journal: {d['journal'] or ''}")
            lines.append("")
        lines.append("## Your lessons learned")
        lines.append(st.get("lessons") or "None yet. You are new. Be patient and careful while you learn.")
        lines.append("\nDecide now and call submit_decision.")
        return "\n".join(lines)

    def _market_block(self, eng, candles, price) -> str:
        s, m = self.s.strategy, eng.market
        out = []
        closes = [c.close for c in candles]
        if candles:
            f, sl = ema(closes, s.fast_ema)[-1], ema(closes, s.slow_ema)[-1]
            vol = atr([c.high for c in candles], [c.low for c in candles], closes, s.atr_period)[-1]
            r = rsi(closes)[-1]
            ch24 = (price / closes[-7] - 1) * 100 if len(closes) >= 7 else 0
            ch7d = (price / closes[-43] - 1) * 100 if len(closes) >= 43 else 0
            out.append(f"ETH/USDT {price:,.2f}. 24h {ch24:+.1f}%, 7d {ch7d:+.1f}%.")
            out.append(f"4h chart: 20 EMA {f:,.2f}, 50 EMA {sl:,.2f}, ATR {vol:,.2f} ({vol / price * 100:.1f}%), RSI {r:.0f}.")
            out.append("Last 30 4h candles (UTC open time, open, high, low, close):")
            for c in candles[-30:]:
                out.append(f"{datetime.fromtimestamp(c.ts, timezone.utc).strftime('%d %b %H:%M')} {c.open:.2f} {c.high:.2f} {c.low:.2f} {c.close:.2f}")
        for label, fn in (("perp", self._perp), ("hourly", self._hourly), ("daily", self._daily), ("btc", self._btc), ("sentiment", self._sentiment)):
            try:
                text = fn(m)
                if text:
                    out.append(text)
            except Exception:
                pass
        return "\n".join(out) + "\n"

    def _perp(self, m) -> str | None:
        p = m.perp_context()
        if not p:
            return None
        yearly = p["funding"] * 24 * 365 * 100
        who = "longs pay shorts" if p["funding"] >= 0 else "shorts pay longs"
        return (f"Hyperliquid ETH perp: mark {p['mark']:,.2f}, funding {p['funding'] * 100:.4f}% per hour "
                f"(about {yearly:+.0f}% a year, {who}), open interest {p['open_interest']:,.0f} ETH, 24h volume ${p['volume_24h'] / 1e6:,.0f}M.")

    def _hourly(self, m) -> str:
        hourly = m.candles(60)
        closes = [c.close for c in hourly]
        r = rsi(closes)[-1]
        f = ema(closes, 20)[-1]
        rows = " | ".join(f"{datetime.fromtimestamp(c.ts, timezone.utc).strftime('%H:%M')} {c.low:.0f}-{c.high:.0f} close {c.close:.0f}" for c in hourly[-24:])
        return f"1h chart: 20 EMA {f:,.2f}, RSI {r:.0f}. Last 24 hours (UTC open time, low-high, close): {rows}"

    def _daily(self, m) -> str:
        daily = m.candles(1440)
        closes = [c.close for c in daily]
        f, sl = ema(closes, 20)[-1], ema(closes, 50)[-1]
        r = rsi(closes)[-1]
        recent = ", ".join(f"{c:.0f}" for c in closes[-30:])
        hi, lo = max(c.high for c in daily[-90:]), min(c.low for c in daily[-90:])
        return (f"Daily chart: 20 EMA {f:,.2f}, 50 EMA {sl:,.2f}, RSI {r:.0f}. 90 day range {lo:,.0f} to {hi:,.0f}.\n"
                f"Last 30 daily closes (oldest first): {recent}")

    def _btc(self, m) -> str:
        btc = m.candles(240, pair="XBTUSDT")
        closes = [c.close for c in btc]
        f, sl = ema(closes, 20)[-1], ema(closes, 50)[-1]
        ch24 = (closes[-1] / closes[-7] - 1) * 100
        ch7d = (closes[-1] / closes[-43] - 1) * 100
        trend = "up" if f > sl else "down"
        return f"BTC/USDT {closes[-1]:,.0f}. 24h {ch24:+.1f}%, 7d {ch7d:+.1f}%. 4h trend {trend} (20 EMA {f:,.0f} vs 50 EMA {sl:,.0f})."

    def _sentiment(self, m) -> str | None:
        fg = m.fear_greed()
        return f"Crypto Fear & Greed Index: {fg[0]} ({fg[1]})." if fg else None

    # Weekly review: this is where Claude learns
    def _review_due(self, eng, now: int) -> bool:
        last = eng.store.get("claude_last_review_ts") or eng.store.get("started_at")
        if now - last < self.c.review_every_days * 86400:
            return False
        return len(eng.store.decisions(100, kind="decision", since=last)) >= 3

    def review(self, eng, key: str, now: int, price: float, tier: dict) -> None:
        st = eng.store
        since = st.get("claude_last_review_ts") or st.get("started_at")
        model = self.c.review_model if tier["name"] == "sharp" else self.c.lean_model
        lines = [f"## Period\n{fmt_time(since)} to {fmt_time(now)}\n", "## Your current lessons", st.get("lessons") or "None yet.", ""]
        eq_then = next(iter(st.equity_history(since)), None)
        if eq_then:
            lines.append(f"## Results\nYour equity went from {eq_then['equity']:.2f} to {eng.equity(price):.2f}. "
                         f"ETH went from {eq_then['price']:,.2f} to {price:,.2f} ({(price / eq_then['price'] - 1) * 100:+.1f}%).")
        if self.rival is not None and self.rival.last_price:
            lines.append(f"The maths bot is at {self.rival.summary()['equity']:.2f}.")
        lines.append(f"You spent ${-st.ledger_total_since('ai_cost', since):.2f} on thinking in this period.\n")
        lines.append("## Your decisions (oldest first)")
        for d in reversed(st.decisions(100, kind="decision", since=since)):
            at = f" at {d['price']:,.2f}" if d["price"] else ""
            lines.append(f"- {fmt_time(d['ts'])}{at}, woken because: {d['wake_reason']}. {(d['action'] or 'none').upper()} "
                         f"({d['confidence'] or '?'}). {d['reasoning']} Journal: {d['journal'] or ''}")
        lines.append("\n## Trades (oldest first)")
        for t in reversed([t for t in st.trades(200) if t["ts"] >= since]):
            pnl = f", P&L {t['pnl']:+.2f}" if t["pnl"] is not None else ""
            lines.append(f"- {fmt_time(t['ts'])} {t['side'].upper()} @ {t['price']:,.2f}{pnl}")
        lines.append("\nReview your week and call submit_review.")
        try:
            content, cost, answered_by = self._call(key, model, REVIEW_PROMPT, "\n".join(lines), [REVIEW_TOOL])
        except Exception as exc:
            st.set("claude_last_review_ts", now - (self.c.review_every_days - 1) * 86400)  # try again tomorrow
            st.log(f"Weekly review failed ({exc}). Will try again tomorrow.", level="error", ts=now)
            return
        eng.charge(now, "ai_cost", cost, f"{answered_by}: weekly review")
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

    # Dashboard
    def extra_summary(self, eng) -> dict:
        now = int(self.clock())
        price = eng.last_price or 0
        tier = self.tier(eng, now, price) if price else None
        last = eng.store.decisions(1, kind="decision")
        return {"brain": {
            "key_set": bool(self.api_key(eng)),
            "style": eng.g.style,
            "key_from_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "tier": tier["name"] if tier else None,
            "model": tier["model"] if tier else None,
            "spent_this_month": self.month_spend(eng, now),
            "budget": self.c.monthly_budget_usd,
            "last_wake": eng.store.get("claude_last_wake_ts"),
            "next_check": eng.store.get("claude_next_check_ts"),
            "lessons": eng.store.get("lessons"),
            "lessons_updated": eng.store.get("lessons_updated"),
            "last_decision": last[0] if last else None,
            "decisions": eng.store.decisions(20),
        }}
