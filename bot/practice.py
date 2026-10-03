"""Practice run: Claude trades real historical moments before it trades for real.

Each scenario is a random moment from a liquid coin's 4h history. The chart is anonymised
(no coin name, no dates, prices rebased so the latest close is 100) so Claude can't
recall what happened next. Code grades each call against what actually followed, then
Claude reviews the whole run and writes the lessons it starts its career with.

Billed to the owner's Anthropic account, not taken from Claude's trading balance."""

import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .claude_brain import knowledge, usage_cost
from .indicators import atr, ema, rsi
from .research import FEE_ROUND_TRIP, MAX_HOLD

HISTORY = 130  # candles shown before the moment
COST_LIMIT = 15.0  # stop early if the run costs more than this

PRACTICE_PROMPT = """You are the trading mind of Survival Bot, doing a practice run before you start trading real money. You will see a real historical 4h chart of a liquid crypto perpetual. The coin's name and the dates are hidden and prices are rebased so the latest close is 100, so you can't know what happened next. The BTC context is from the same moment.

Decide what you would do right now, exactly as you would in live trading: go long, go short, or pass. If you trade, give your stop and target as a percentage distance from the entry at 100, your honest probability that the target is hit before the stop, and the setup you're using. Trades are closed after 5 days if neither is hit. Fees are about 0.13% per round trip. Passing is often the right answer.

Your playbook:
{playbook}

Backtested setup statistics:
{stats}

Call submit_practice_trade once."""

PRACTICE_TOOL = {
    "name": "submit_practice_trade",
    "description": "Your decision for this historical moment. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["long", "short", "pass"]},
            "setup": {"type": ["string", "null"], "description": "breakout, pullback, range_fade, squeeze_fade, failed_breakout, momentum or other"},
            "stop_pct": {"type": ["number", "null"], "description": "Stop distance from entry, in percent."},
            "target_pct": {"type": ["number", "null"], "description": "Target distance from entry, in percent."},
            "win_probability": {"type": "number", "description": "Your honest probability (0 to 100) that the target is hit before the stop."},
            "reasoning": {"type": "string", "description": "One or two sentences."},
        },
        "required": ["action", "setup", "stop_pct", "target_pct", "win_probability", "reasoning"],
        "additionalProperties": False,
    },
}

REVIEW_PROMPT = """You are the trading mind of Survival Bot. You just finished a practice run on anonymised historical charts, before trading real money. Below are your decisions, how each one turned out, and summary statistics including how well calibrated your confidence was.

Be a tough, honest coach to yourself. Where did you have an edge and where didn't you? Which setups worked? Were you over- or under-confident? Did you pass on moves you should have taken, or take trades you should have passed? Then write the lessons you'll carry into live trading: short, specific, actionable rules (at most 12 bullet points, under 300 words). Call submit_practice_review once."""

REVIEW_TOOL = {
    "name": "submit_practice_review",
    "description": "Summary and lessons from the practice run. Call exactly once.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Plain English summary for your owner, 3 to 5 sentences."},
            "lessons": {"type": "string", "description": "Your lessons for live trading, as bullet points."},
        },
        "required": ["summary", "lessons"],
        "additionalProperties": False,
    },
}


def rebase(candles, base: float):
    k = 100.0 / base
    return [(c.open * k, c.high * k, c.low * k, c.close * k) for c in candles]


def scenario_text(candles, i: int, btc) -> str:
    window = candles[i - HISTORY + 1:i + 1]
    closes = [c.close for c in candles[:i + 1]]
    f, s = ema(closes, 20)[-1], ema(closes, 50)[-1]
    a = atr([c.high for c in candles[:i + 1]], [c.low for c in candles[:i + 1]], closes, 14)[-1]
    base = closes[-1]
    k = 100.0 / base
    hi = max(c.high for c in candles[i - 120:i]) * k
    lo = min(c.low for c in candles[i - 120:i]) * k
    rows = rebase(window[-40:], base)
    lines = [f"Coin X, latest 4h close = 100.00. 24h {(closes[-1] / closes[-7] - 1) * 100:+.1f}%, 7d {(closes[-1] / closes[-43] - 1) * 100:+.1f}%, "
             f"30d {(closes[-1] / closes[-181] - 1) * 100 if len(closes) > 181 else 0:+.1f}%.",
             f"4h: 20 EMA {f * k:.2f}, 50 EMA {s * k:.2f}, RSI {rsi(closes)[-1]:.0f}, ATR {a * k:.2f} ({a / base * 100:.1f}%). "
             f"20-day high {hi:.2f}, 20-day low {lo:.2f}.",
             "Last 40 4h candles, oldest first (open high low close):"]
    lines += [f"{o:.2f} {h:.2f} {l:.2f} {c:.2f}" for o, h, l, c in rows]
    if btc:
        bc = [c.close for c in btc]
        bf, bs = ema(bc, 20)[-1], ema(bc, 50)[-1]
        lines.append(f"BTC at the same moment: 24h {(bc[-1] / bc[-7] - 1) * 100:+.1f}%, 7d {(bc[-1] / bc[-43] - 1) * 100:+.1f}%, "
                     f"4h trend {'up' if bf > bs else 'down'}, RSI {rsi(bc)[-1]:.0f}.")
    return "\n".join(lines)


def grade(candles, i: int, d: dict) -> dict:
    """What actually happened after the decision."""
    entry = candles[i].close
    future = candles[i + 1:i + 1 + MAX_HOLD]
    best_up = max((c.high for c in future), default=entry) / entry * 100 - 100
    best_down = 100 - min((c.low for c in future), default=entry) / entry * 100
    out = {"move_up_pct": best_up, "move_down_pct": best_down, "end_pct": (future[-1].close / entry - 1) * 100 if future else 0}
    if d["action"] not in ("long", "short") or not d.get("stop_pct") or not d.get("target_pct"):
        return {**out, "taken": False}
    stop_pct = min(max(float(d["stop_pct"]), 0.3), 30)
    target_pct = max(float(d["target_pct"]), 0.1)
    direction = 1 if d["action"] == "long" else -1
    stop = entry * (1 - direction * stop_pct / 100)
    target = entry * (1 + direction * target_pct / 100)
    fee_r = FEE_ROUND_TRIP * 100 / stop_pct
    r, how = None, "time"
    for c in future:
        if (direction > 0 and c.low <= stop) or (direction < 0 and c.high >= stop):
            r, how = -1.0, "stop"
            break
        if (direction > 0 and c.high >= target) or (direction < 0 and c.low <= target):
            r, how = target_pct / stop_pct, "target"
            break
    if r is None:
        r = direction * (future[-1].close / entry - 1) * 100 / stop_pct if future else 0.0
    return {**out, "taken": True, "r": r - fee_r, "exit": how, "won": how == "target" or (how == "time" and r > fee_r)}


class PracticeRun:
    def __init__(self, brain, store, key: str, market, scenarios: int = 60, seed: int | None = None, workers: int = 4):
        self.brain, self.store, self.key, self.market = brain, store, key, market
        self.n = scenarios
        self.rng = random.Random(seed)
        self.workers = workers
        self.cost = 0.0
        self.lock = threading.Lock()
        self.model = brain.c.smart_model

    def status(self, **kw) -> None:
        s = self.store.get("practice_status") or {}
        s.update(kw)
        self.store.set("practice_status", s)

    def start(self) -> threading.Thread:
        self.status(state="running", done=0, total=self.n, cost=0.0, started=int(time.time()), message="Loading history")
        t = threading.Thread(target=self._run_safely, daemon=True)
        t.start()
        return t

    def _run_safely(self) -> None:
        try:
            self.run()
        except Exception as exc:
            self.status(state="failed", message=f"{type(exc).__name__}: {exc}", cost=self.cost)
            self.store.log(f"Practice run failed ({exc}). Spent ${self.cost:.2f}.", level="error")

    def _load(self):
        coins = self.market.liquid(50e6) or ["BTC", "ETH", "SOL"]
        history = {}
        for coin in coins + (["BTC"] if "BTC" not in coins else []):
            c = self.market.candles(240, coin=coin, count=5000)
            if len(c) > HISTORY + MAX_HOLD + 200:
                history[coin] = c
        return history

    def _pick(self, history):
        picks = []
        coins = [c for c in history if c != "BTC" or len(history) == 1]
        for _ in range(self.n):
            coin = self.rng.choice(coins)
            c = history[coin]
            i = self.rng.randrange(max(HISTORY, 200), len(c) - MAX_HOLD - 1)
            picks.append((coin, i))
        return picks

    def _btc_at(self, history, ts):
        btc = history.get("BTC")
        if not btc:
            return None
        idx = next((k for k in range(len(btc) - 1, -1, -1) if btc[k].ts <= ts), None)
        return btc[max(0, idx - 60):idx + 1] if idx and idx > 60 else None

    def _ask(self, system, text):
        content, cost, _ = self.brain._call(self.key, self.model, system, text, [PRACTICE_TOOL])
        with self.lock:
            self.cost += cost
        call = next((b for b in content if getattr(b, "type", None) == "tool_use" and b.name == "submit_practice_trade"), None)
        if call is None:
            return {"action": "pass", "setup": None, "stop_pct": None, "target_pct": None, "win_probability": 0, "reasoning": "No answer."}
        return call.input if isinstance(call.input, dict) else json.loads(call.input)

    def run(self) -> None:
        history = self._load()
        picks = self._pick(history)
        system = PRACTICE_PROMPT.format(playbook=knowledge("playbook.md"), stats=knowledge("setup_stats.md") or "None yet.")
        results = []
        self.status(message="Trading historical moments")

        def one(pick):
            coin, i = pick
            if self.cost >= COST_LIMIT:
                return None
            c = history[coin]
            d = self._ask(system, scenario_text(c, i, self._btc_at(history, c[i].ts)))
            g = grade(c, i, d)
            with self.lock:
                results.append({"coin": coin, **d, **g})
                self.status(done=len(results), cost=round(self.cost, 3))
            return True

        with ThreadPoolExecutor(self.workers) as pool:
            list(pool.map(one, picks))

        summary = summarise(results)
        self.status(message="Reviewing the run")
        lessons, review = self._review(results, summary)
        self.store.set("practice_lessons", lessons)
        self.store.set("practice_report", {"summary": review, "stats": summary, "results": results[-200:]})
        self.status(state="done", cost=round(self.cost, 3), message=review, finished=int(time.time()))
        self.store.log(f"Practice run finished: {summary['taken']} trades from {summary['scenarios']} moments, "
                       f"{summary['total_r']:+.1f}R in total ({summary['expectancy']:+.2f}R per trade), cost ${self.cost:.2f}. {review}",
                       level="thought")

    def _review(self, results, summary):
        lines = [json.dumps(summary, indent=1), "", "Each decision (coin hidden from you at the time is shown here):"]
        for r in results:
            if r["taken"]:
                lines.append(f"- {r['coin']}: {r['action']} {r.get('setup')}, stop {r['stop_pct']}%, target {r['target_pct']}%, "
                             f"you said {r['win_probability']:.0f}% -> {r['exit']}, {r['r']:+.2f}R. Reasoning: {r['reasoning']}")
            else:
                lines.append(f"- {r['coin']}: passed. Next 5 days: up to +{r['move_up_pct']:.1f}%, down to -{r['move_down_pct']:.1f}%, "
                             f"ended {r['end_pct']:+.1f}%. Reasoning: {r['reasoning']}")
        content, cost, _ = self.brain._call(self.key, self.model, REVIEW_PROMPT, "\n".join(lines), [REVIEW_TOOL])
        self.cost += cost
        call = next((b for b in content if getattr(b, "type", None) == "tool_use" and b.name == "submit_practice_review"), None)
        if call is None:
            return "", "The review didn't return lessons."
        r = call.input if isinstance(call.input, dict) else json.loads(call.input)
        return r["lessons"], r["summary"]


def summarise(results) -> dict:
    taken = [r for r in results if r["taken"]]
    out = {"scenarios": len(results), "taken": len(taken), "passed": len(results) - len(taken),
           "total_r": sum(r["r"] for r in taken), "expectancy": sum(r["r"] for r in taken) / len(taken) if taken else 0.0,
           "win_rate": sum(1 for r in taken if r["won"]) / len(taken) * 100 if taken else 0.0}
    by_setup = {}
    for r in taken:
        b = by_setup.setdefault(r.get("setup") or "other", [])
        b.append(r["r"])
    out["by_setup"] = {k: {"trades": len(v), "expectancy": sum(v) / len(v)} for k, v in by_setup.items()}
    calib = {}
    for lo, hi, name in ((0, 45, "under 45%"), (45, 60, "45 to 60%"), (60, 101, "over 60%")):
        b = [r for r in taken if lo <= r["win_probability"] < hi]
        if b:
            calib[name] = {"trades": len(b), "stated": sum(r["win_probability"] for r in b) / len(b),
                           "actual": sum(1 for r in b if r["won"]) / len(b) * 100}
    out["calibration"] = calib
    big = [r for r in results if not r["taken"] and max(r["move_up_pct"], r["move_down_pct"]) >= 10]
    out["passed_on_10pct_moves"] = len(big)
    return out
