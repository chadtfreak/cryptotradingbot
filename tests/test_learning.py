"""Shadow calls, trade records, quick trade reviews and lean calls in practice."""

from bot import learning
from bot.practice import grade, grade_lean, summarise
from bot.store import Store
from tests.test_bot import H, make_candles
from tests.test_claude import tool_reply
from tests.test_multi import actions, make_multi, wake


def call(coin="SOL", direction="long", stop=19.0, target=22.0, hours=24, prob=60, setup="breakout"):
    return {"coin": coin, "direction": direction, "stop_price": stop, "target_price": target, "hours": hours,
            "probability": prob, "setup": setup}


def test_calls_on_wrong_side_are_dropped():
    st = Store(":memory:")
    labels = learning.add_calls(st, [call(), call(stop=21.0), call(direction="short", stop=19.0, target=18.0), call(coin="NOPE")],
                                {"SOL": 20.0}, 0)
    assert labels == ["SOL up"] and len(st.get("shadow_calls")) == 1


def test_calls_marked_on_target_stop_and_time():
    st = Store(":memory:")
    learning.add_calls(st, [call(), call(direction="short", stop=21.0, target=18.0), call(coin="BTC", stop=900.0, target=1200.0, hours=4)],
                       {"SOL": 20.0, "BTC": 1000.0}, 0)
    assert learning.grade_calls(st, {"SOL": 20.5, "BTC": 1000.0}, H) == []
    done = learning.grade_calls(st, {"SOL": 22.0, "BTC": 1050.0}, 5 * H)
    by = {(d["coin"], d["dir"]): d for d in done}
    assert by[("SOL", 1)]["exit"] == "target" and by[("SOL", 1)]["r"] == 2.0
    assert by[("SOL", -1)]["exit"] == "stop" and by[("SOL", -1)]["r"] == -1.0
    assert by[("BTC", 1)]["exit"] == "time" and by[("BTC", 1)]["r"] == 0.5
    s = learning.stats(st)
    assert s["calls"]["n"] == 3 and s["open_calls"] == 0 and "breakout" in s["calls_by_setup"]


def test_live_decision_saves_calls_and_context_shows_scorecard():
    d = actions()
    d["calls"] = [call(stop=19.0, target=22.0), call(coin="BTC", direction="short", stop=1100.0, target=900.0, setup="momentum")]
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", d), tool_reply("submit_decisions", actions())])
    eng.tick()
    assert len(eng.store.get("shadow_calls")) == 2
    assert "shadow calls: SOL up, BTC down" in eng.store.decisions(1)[0]["action"]
    tool = next(t for t in fake.requests[0]["tools"] if t.get("name") == "submit_decisions")
    assert "calls" in tool["input_schema"]["required"]
    market.px["SOL"] = 22.5
    wake(eng, clock)
    assert eng.store.get("shadow_results")[0]["exit"] == "target"
    ctx = fake.requests[1]["messages"][0]["content"]
    assert "## Your scorecard" in ctx and "Shadow calls: 1 marked" in ctx


def test_closed_trade_recorded_in_r_and_reviewed_once():
    review = {"verdict": "good trade", "lesson": "Breakouts with BTC behind them work."}
    eng, fake, market, clock = make_multi([
        tool_reply("submit_decisions", actions(("long", "SOL", 40, 19.0, None))),
        tool_reply("submit_decisions", actions(("close", "SOL", None, None, 50))),
        tool_reply("submit_decisions", actions(("close", "SOL", None, None, 100))),
        tool_reply("submit_trade_review", review, model="claude-sonnet-5-5"),
        tool_reply("submit_decisions", actions())])
    eng.tick()
    risk = eng.positions["SOL"]["risk_usd"]
    assert risk > 0
    market.px["SOL"] = 22.0
    wake(eng, clock)
    assert eng.store.get("trade_records") is None  # half closed: not finished yet
    wake(eng, clock)
    rec = eng.store.get("trade_records")[0]
    assert rec["setup"] == "breakout" and rec["r"] > 1 and rec["pnl"] > 0
    clock.t += 60
    eng.tick()  # the review happens on the next tick
    assert fake.requests[3]["model"] == "claude-sonnet-5-5"
    assert eng.store.get("trade_lessons")[0]["lesson"] == review["lesson"]
    assert eng.store.get("trade_reviews_pending") == []
    assert "good trade" in learning.scorecard(eng.store)


def test_stop_hit_also_recorded():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 30, 19.0, None)))])
    eng.tick()
    eng.store.set("anthropic_api_key", None)  # no review call, just the record
    market.px["SOL"] = 18.5
    clock.t += 60
    eng.tick()
    rec = eng.store.get("trade_records")[0]
    assert rec["r"] < -1 and "Stop hit" in rec["reason"]


def test_lean_call_grading():
    c = make_candles([100] * 20 + [100, 103, 106] + [106] * 30, spread=0.5)
    assert grade_lean(c, 20, "up", 5.0) is True
    assert grade_lean(c, 20, "down", 5.0) is False
    assert grade_lean(c, 20, "up", 50.0) is None
    g = grade(c, 20, {"action": "pass", "stop_pct": None, "target_pct": None, "lean": "up"})
    assert "lean_right" in g and g["lean_size"] > 0


def test_summarise_lean_calls():
    rs = [{"taken": False, "move_up_pct": 3, "move_down_pct": 1, "end_pct": 2, "lean_right": True, "lean_probability": 65},
          {"taken": False, "move_up_pct": 3, "move_down_pct": 1, "end_pct": 2, "lean_right": False, "lean_probability": 55},
          {"taken": True, "r": 1.0, "won": True, "win_probability": 60, "setup": "breakout", "lean_right": True, "lean_probability": 75}]
    s = summarise(rs)["lean_calls"]
    assert s["marked"] == 3 and round(s["right_pct"]) == 67 and s["right_pct_when_passed"] == 50
    assert s["by_confidence"]["70% or more"]["right_pct"] == 100


# Learning phase

def learning_engine(budget=40.0, days=30):
    from tests.test_multi import make_multi as mm
    d = actions()
    eng, fake, market, clock = mm([tool_reply("submit_decisions", d)] * 5)
    eng.brain.c.learning_phase_budget_usd, eng.brain.c.learning_phase_days = budget, days
    return eng, fake, market, clock


def test_learning_phase_pays_for_thinking_and_uses_smart_model():
    eng, fake, market, clock = learning_engine()
    eng.store.set("allowance", {"month": "2027-01", "amount": 6.0, "smart": False, "why": "Beat neither."})
    cash = eng.cash
    eng.tick()
    assert fake.requests[0]["model"] == "claude-opus-5-5"
    assert eng.cash == cash  # not charged to the bot
    assert eng.store.ledger_total("ai_sponsored") < 0 and eng.store.ledger_total("ai_cost") == 0
    tier = eng.brain.tier(eng, clock.t)
    assert tier["learning"] and tier["budget"] == 40.0 and tier["name"] == "sharp"
    assert eng.summary()["brain"]["learning"]["active"]


def test_learning_phase_ends_after_its_days():
    eng, fake, market, clock = learning_engine(days=1)
    eng.tick()
    clock.t += 2 * 86400
    eng.store.set("claude_next_check_ts", clock.t - 1)
    cash = eng.cash
    eng.tick()
    assert eng.cash < cash  # back to paying its own way
    assert eng.store.get("learning_phase_ended") and "Learning phase over" in " ".join(e["message"] for e in eng.store.events())
    assert not eng.brain.tier(eng, clock.t).get("learning")


def test_learning_phase_ends_when_budget_used():
    eng, fake, market, clock = learning_engine(budget=1.0)
    eng.tick()
    eng.store.add_ledger(clock.t, "ai_sponsored", -0.9, "practice")
    assert not eng.brain.learning(eng, clock.t)["active"]


def test_no_learning_phase_by_default():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())])
    eng.tick()
    assert eng.brain.learning(eng, clock.t) is None and eng.store.ledger_total("ai_cost") < 0


def test_claude_can_check_back_in_15_minutes_and_sees_its_pace():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(hours=0.25))] * 3)
    eng.brain.c.min_check_hours, eng.brain.c.min_minutes_between_wakes = 0.25, 15
    eng.tick()
    clock.t += 10 * 60
    eng.tick()
    assert len(fake.requests) == 1
    clock.t += 6 * 60
    eng.tick()
    assert len(fake.requests) == 2
    ctx = fake.requests[1]["messages"][0]["content"]
    assert "Scheduled check-in" in ctx and "- Pace: 1 checks in the last 24 hours" in ctx
    assert "no limit on how often you trade" in fake.requests[1]["system"]
