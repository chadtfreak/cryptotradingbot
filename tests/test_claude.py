"""Claude brain tests. A fake client stands in for the Anthropic API, so nothing is spent."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bot.claude_brain import ClaudeBrain, usage_cost
from bot.engine import Engine
from bot.store import Store
from bot.web import create_app
from tests.test_bot import Clock, FakeMarket, H, cross_up_series, settings


def usage(inp=5000, out=1500, searches=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=0))


def tool_reply(name, data, model="claude-opus-5-5", stop="tool_use", **u):
    block = SimpleNamespace(type="tool_use", name=name, input=data, id="t1")
    return SimpleNamespace(content=[SimpleNamespace(type="text", text="thinking out loud"), block],
                           stop_reason=stop, model=model, usage=usage(**u))


class FakeClaude:
    """Records every request and replays queued responses."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **params):
        self.requests.append(params)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def decision(action="hold", pct=None, stop=None, hours=24, confidence="medium", sell_pct=None):
    return {"action": action, "position_pct": pct, "sell_pct": sell_pct, "stop_price": stop, "confidence": confidence,
            "reasoning": f"Test reasoning for {action}.", "journal": "Expect X.", "next_check_hours": hours}


class MarketPlus(FakeMarket):
    def candles(self, interval, pair=None):
        return self._candles

    def fear_greed(self):
        return (50, "Neutral")


def full_send():
    from bot.config import GuardrailSettings
    return GuardrailSettings(style="full send", max_risk_per_trade=0.25, min_stop_distance_pct=0.5, max_stop_distance_pct=50,
                             max_trades_per_day=0, daily_loss_limit_pct=0, allow_adding=True, stops_only_up=False)


def make(replies, closes=None, price=None, key="sk-ant-test", guardrails=None):
    s = settings()
    clock = Clock()
    fake = FakeClaude(replies)
    brain = ClaudeBrain(s, client_factory=lambda k: fake, clock=clock)
    market = MarketPlus(closes or [100.0] * 80, price)
    eng = Engine(s, market, Store(":memory:"), clock=clock, brain=brain, guardrails=guardrails)
    if key:
        eng.store.set("anthropic_api_key", key)
    return eng, fake, market, clock


def test_usage_cost_prices_opus_and_search():
    # 5000 in at $4/M + 1500 out at $20/M + 2 searches at 1c
    assert usage_cost(usage(5000, 1500, 2), "claude-opus-5-5") == pytest.approx(0.02 + 0.03 + 0.02)
    assert usage_cost(usage(5000, 1500), "claude-sonnet-5-5") == pytest.approx(0.01 + 0.015)


def test_waits_for_key_without_calling():
    eng, fake, *_ = make([], key=None)
    eng.tick()
    assert fake.requests == []
    assert "API key" in eng.store.events()[0]["message"]


def test_first_wake_hold_charges_cost_and_schedules_next_check():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("hold", hours=6))])
    eng.tick()
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["fallbacks"] == "default"
    assert any(t.get("name") == "web_search" for t in req["tools"])
    assert "Why you were woken" in req["messages"][0]["content"]
    cost = usage_cost(usage(), "claude-opus-5-5")
    assert eng.cash == pytest.approx(100 - cost)
    assert eng.store.get("claude_next_check_ts") == clock.t + 6 * H
    # Nothing new happened, so the next tick does not wake Claude
    clock.t += 60
    eng.tick()
    assert len(fake.requests) == 1


def test_buy_goes_through_guardrails():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("buy", pct=90, stop=95))])
    eng.tick()
    [t] = eng.store.trades()
    assert t["side"] == "buy"
    # Stop 5% below: 3% max risk means at most 60% of equity, not the 90% asked for
    assert t["notional"] == pytest.approx(0.03 * eng.equity(100) / 0.05, rel=0.02)
    assert eng.store.get("stop") == 95
    assert any("trimmed" in e["message"] for e in eng.store.events())


def test_buy_without_valid_stop_is_rejected():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("buy", pct=50, stop=99.9))])
    eng.tick()
    assert eng.store.trades() == []
    assert any("Guardrail: buy rejected" in e["message"] for e in eng.store.events())


def test_stop_can_only_move_up():
    eng, fake, market, clock = make([
        tool_reply("submit_decision", decision("buy", pct=50, stop=95)),
        tool_reply("submit_decision", decision("set_stop", stop=90)),
        tool_reply("submit_decision", decision("set_stop", stop=97)),
    ])
    eng.tick()
    for _ in range(2):
        clock.t += 25 * H  # past the heartbeat
        eng.store.set("claude_next_check_ts", None)
        eng.tick()
    assert eng.store.get("stop") == 97
    assert any("stops only move up" in e["message"] for e in eng.store.events())


def test_big_move_wakes_claude():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("hold", hours=48))] * 2)
    eng.tick()
    clock.t += 2 * H
    market.set(price=104)  # +4%
    eng.tick()
    assert len(fake.requests) == 2
    assert "moved +4.0%" in fake.requests[1]["messages"][0]["content"]


def test_lean_mode_when_health_low_uses_cheaper_model_without_search():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("hold"), model="claude-sonnet-5-5")])
    eng.store.set("cash", 70.0)  # health 40%
    eng.store.set("day_start_equity", 70.0)
    eng.tick()
    req = fake.requests[0]
    assert req["model"] == "claude-sonnet-5-5"
    assert not any(t.get("name") == "web_search" for t in req["tools"])


def test_asleep_when_budget_used():
    eng, fake, market, clock = make([])
    eng.store.add_ledger(clock.t, "ai_cost", -14.8, "earlier thinking")
    eng.tick()
    assert fake.requests == []
    assert any("budget for this month is used up" in e["message"] for e in eng.store.events())


def test_api_error_backs_off_and_does_not_crash():
    eng, fake, market, clock = make([RuntimeError("overloaded"), tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    assert eng.last_error is None
    assert any("Couldn't reach Claude" in e["message"] for e in eng.store.events())
    clock.t += 5 * 60
    market.set(price=110)
    eng.tick()  # still inside the 15 minute back-off
    assert len(fake.requests) == 1
    clock.t += 11 * 60
    eng.tick()  # retries the first look, not tomorrow's heartbeat
    assert len(fake.requests) == 2
    assert "First look" in fake.requests[1]["messages"][0]["content"]


def test_no_tool_call_means_hold():
    reply = SimpleNamespace(content=[SimpleNamespace(type="text", text="hmm")], stop_reason="end_turn",
                            model="claude-opus-5-5", usage=usage())
    eng, fake, *_ = make([reply])
    eng.tick()
    assert eng.store.trades() == []
    assert eng.store.decisions(1)[0]["reasoning"] == "No decision returned."


def test_refusal_is_logged_not_crashed():
    reply = SimpleNamespace(content=[], stop_reason="refusal", model="claude-opus-5-5", usage=usage(0, 0))
    eng, fake, *_ = make([reply])
    eng.tick()
    assert any("declined" in e["message"] for e in eng.store.events())


def test_pause_turn_is_resumed():
    paused = SimpleNamespace(content=[SimpleNamespace(type="server_tool_use")], stop_reason="pause_turn",
                             model="claude-opus-5-5", usage=usage(1000, 100, searches=1))
    eng, fake, *_ = make([paused, tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    assert len(fake.requests) == 2
    assert fake.requests[1]["messages"][1]["role"] == "assistant"
    assert eng.store.decisions(1)[0]["cost"] == pytest.approx(usage_cost(usage(1000, 100, 1), "claude-opus-5-5") + usage_cost(usage(), "claude-opus-5-5"))


def test_falls_back_without_fallbacks_param_if_rejected():
    eng, fake, *_ = make([RuntimeError("fallbacks: unsupported beta"), tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    assert "fallbacks" not in fake.requests[1]
    assert len(eng.store.decisions(5)) == 1


def test_weekly_review_updates_lessons():
    replies = [tool_reply("submit_decision", decision("hold", hours=24))] * 3
    replies.append(tool_reply("submit_review", {"summary": "Quiet week.", "lessons": "- Be patient."}))
    replies.append(tool_reply("submit_decision", decision("hold")))
    eng, fake, market, clock = make(replies)
    for _ in range(3):
        eng.tick()
        clock.t += 25 * H
    clock.t += 5 * 86400
    eng.tick()
    assert eng.store.get("lessons") == "- Be patient."
    assert fake.requests[3]["tools"][0]["name"] == "submit_review"
    # The next decision sees the lessons
    assert "- Be patient." in fake.requests[4]["messages"][0]["content"]


def test_dashboard_never_shows_key():
    eng, *_ = make([tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    client = TestClient(create_app({"claude": eng}, run_loop=False))
    body = json.dumps(client.get("/api/claude/status").json())
    assert "sk-ant-test" not in body
    assert '"key_set": true' in body


def test_key_endpoint_rejects_junk(monkeypatch):
    eng, *_ = make([], key=None)
    client = TestClient(create_app({"claude": eng}, run_loop=False))
    assert client.post("/api/claude/key", json={"key": "hello"}).status_code == 400
    monkeypatch.setattr("bot.web.check_key", lambda k: (True, "ok"))
    assert client.post("/api/claude/key", json={"key": "sk-ant-abc"}).json()["brain"]["key_set"]


# Full send mode

def wake_again(eng, clock):
    clock.t += 2 * H
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()


def test_full_send_prompt_matches_rules():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("hold"))], guardrails=full_send())
    eng.tick()
    system = fake.requests[0]["system"]
    assert "aggressive" in system
    assert "No limit on trades per day." in system
    assert "No daily loss limit." in system
    assert "up or down" in system
    assert "25% of equity" in system


def test_careful_prompt_for_careful_rules():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    assert "Capital preservation" in fake.requests[0]["system"]
    assert "Stops can only move up." in fake.requests[0]["system"]


def test_full_send_big_size_add_partial_sell_and_lower_stop():
    eng, fake, market, clock = make([
        tool_reply("submit_decision", decision("buy", pct=50, stop=90)),   # 50% with a 10% stop risks 5%: allowed
        tool_reply("submit_decision", decision("buy", pct=30, stop=92)),   # add to it
        tool_reply("submit_decision", decision("set_stop", stop=85)),      # lowering is allowed
        tool_reply("submit_decision", decision("sell", sell_pct=50)),      # take half off
    ], guardrails=full_send())
    eng.tick()
    first = eng.store.trades()[0]
    assert first["notional"] == pytest.approx(50, rel=0.02)
    wake_again(eng, clock)
    assert len(eng.store.trades()) == 2 and eng.store.get("stop") == 92
    held = eng.qty
    wake_again(eng, clock)
    assert eng.store.get("stop") == 85
    wake_again(eng, clock)
    assert eng.qty == pytest.approx(held / 2)
    last = eng.store.trades()[0]
    assert last["side"] == "sell" and last["pnl"] is not None
    assert eng.store.get("cost_basis") > 0


def test_full_send_has_no_daily_loss_limit_or_trade_cap():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("buy", pct=90, stop=60))], guardrails=full_send())
    eng.tick()
    market.set(price=80)  # down about 18% in a day, still above the stop and floor
    clock.t += 60
    eng.tick()
    assert eng.status == "running" and eng.qty > 0
    assert eng.g.max_trades_per_day == 0


def test_floor_still_kills_full_send():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("buy", pct=50, stop=60))], guardrails=full_send())
    eng.tick()
    # Earlier losses have left it with little cash; this drop takes equity under the floor
    eng.store.set("cash", 20.0)
    market.set(price=59)  # equity 20 + 0.5 x 59 = 49.5, under the floor
    clock.t += 60
    eng.tick()
    assert eng.status == "dead" and eng.qty == 0


def test_risk_cap_limits_full_send_losses():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("buy", pct=95, stop=50))], guardrails=full_send())
    eng.tick()
    market.set(price=45)  # gaps straight through the stop
    clock.t += 60
    eng.tick()
    assert eng.status == "running" and eng.qty == 0
    assert 65 < eng.equity(45) < 80  # roughly the 25% risk cap, plus a little gap slippage
