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
    name = "submit_decisions" if name == "submit_decision" else name
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
        if isinstance(params.get("system"), list):  # cached system prompt: keep the blocks, expose the text for asserts
            params = {**params, "system_blocks": params["system"], "system": "".join(b["text"] for b in params["system"])}
        self.requests.append(params)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def decision(action="hold", pct=None, stop=None, hours=24, confidence="medium", sell_pct=None, coin="ETH", close_pct=None, setup="breakout"):
    actions = []
    if action != "hold":
        act = {"buy": "long", "sell": "close", "raise_stop": "set_stop"}.get(action, action)
        actions.append({"action": act, "coin": coin, "position_pct": pct, "close_pct": close_pct or sell_pct,
                        "stop_price": stop, "setup": setup})
    return {"actions": actions, "confidence": confidence, "reasoning": f"Test reasoning for {action}.",
            "journal": "Expect X.", "next_check_hours": hours}


class MarketPlus(FakeMarket):
    def candles(self, interval, coin=None, pair=None, count=300):
        return self._candles

    def fear_greed(self):
        return (50, "Neutral")


def full_send():
    from bot.config import GuardrailSettings
    return GuardrailSettings(style="full send", max_risk_per_trade=0.25, min_stop_distance_pct=0.5, max_stop_distance_pct=50,
                             max_trades_per_day=0, daily_loss_limit_pct=0, allow_adding=True, stops_only_up=False,
                             allow_short=True, max_leverage=1.0)


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
    assert (eng.position() or {}).get("stop") == 95
    assert any("trimmed" in e["message"] for e in eng.store.events())


def test_buy_without_valid_stop_is_rejected():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("buy", pct=50, stop=99.9))])
    eng.tick()
    assert eng.store.trades() == []
    assert any("buy rejected" in e["message"] for e in eng.store.events())


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
    assert (eng.position() or {}).get("stop") == 97
    assert any("can only be tightened" in e["message"] for e in eng.store.events())


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
    assert "either direction" in system
    assert "You can go short." in system
    assert "25% of equity" in system


def test_careful_prompt_for_careful_rules():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    assert "Capital preservation" in fake.requests[0]["system"]
    assert "Stops can only be tightened." in fake.requests[0]["system"]
    assert "No shorting" in fake.requests[0]["system"]


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
    assert len(eng.store.trades()) == 2 and (eng.position() or {}).get("stop") == 92
    held = eng.qty
    wake_again(eng, clock)
    assert (eng.position() or {}).get("stop") == 85
    wake_again(eng, clock)
    assert eng.qty == pytest.approx(held / 2)
    last = eng.store.trades()[0]
    assert last["side"] == "sell" and last["pnl"] is not None
    assert eng.position()["cost_basis"] > 0


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


# Shorting

def test_short_profits_when_price_falls():
    eng, fake, market, clock = make([
        tool_reply("submit_decision", decision("short", pct=40, stop=110)),
        tool_reply("submit_decision", decision("close", close_pct=100)),
    ], guardrails=full_send())
    eng.tick()
    assert eng.qty < 0 and (eng.position() or {}).get("stop") == 110
    assert eng.cash > 100  # sale proceeds sit in cash
    assert 99.7 < eng.equity(100) < 100  # only thinking cost, fees and slippage lost so far
    market.set(price=90)
    wake_again(eng, clock)
    assert eng.qty == 0
    close = eng.store.trades()[0]
    assert close["pnl"] == pytest.approx(0.10 * 40, rel=0.1)  # 10% move on a $40 short, less fees


def test_short_stop_triggers_when_price_rises():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("short", pct=40, stop=105))], guardrails=full_send())
    eng.tick()
    market.set(price=106)
    clock.t += 60
    eng.tick()
    assert eng.qty == 0
    assert "rose to or above" in eng.store.trades()[0]["reason"]
    assert eng.store.trades()[0]["pnl"] < 0


def test_short_stop_must_be_above_price():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("short", pct=40, stop=95))], guardrails=full_send())
    eng.tick()
    assert eng.qty == 0
    assert any("short rejected" in e["message"] for e in eng.store.events())


def test_flip_from_long_to_short():
    eng, fake, market, clock = make([
        tool_reply("submit_decision", decision("long", pct=40, stop=95)),
        tool_reply("submit_decision", decision("short", pct=40, stop=105)),
    ], guardrails=full_send())
    eng.tick()
    assert eng.qty > 0
    wake_again(eng, clock)
    assert eng.qty < 0
    sides = [t["side"] for t in reversed(eng.store.trades())]
    assert sides == ["buy", "sell", "sell"]  # buy long, sell to close, sell to open short


def test_no_leverage_on_shorts():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("short", pct=300, stop=150))], guardrails=full_send())
    eng.tick()
    assert abs(eng.qty) * 100 <= eng.equity(100) * 1.0 + 0.01


def test_careful_bot_cannot_short():
    eng, fake, *_ = make([tool_reply("submit_decision", decision("short", pct=40, stop=105))])
    eng.tick()
    assert eng.qty == 0
    assert any("isn't allowed to short" in e["message"] for e in eng.store.events())


def test_funding_paid_by_longs_and_received_by_shorts():
    for action, stop, sign in (("long", 95, -1), ("short", 105, 1)):
        eng, fake, market, clock = make([tool_reply("submit_decision", decision(action, pct=50, stop=stop))], guardrails=full_send())
        market.funding = 0.0001  # 0.01% an hour
        eng.tick()
        clock.t += 3600
        eng.tick()
        assert eng.summary()["funding_paid"] == pytest.approx(-sign * 50 * 0.0001, rel=0.05)


def test_funding_starts_for_position_opened_before_upgrade():
    eng, fake, market, clock = make([tool_reply("submit_decision", decision("long", pct=50, stop=95))], guardrails=full_send())
    eng.tick()
    eng.store.set("last_funding_ts", None)  # as if the position predates funding support
    market.funding = 0.0001
    for _ in range(3):
        clock.t += 3600
        eng.tick()
    assert eng.summary()["funding_paid"] > 0


# Prompt caching

def test_system_prompt_is_cached_and_ttl_follows_how_often_claude_checks():
    from tests.test_multi import actions, make_multi
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(hours=6))] * 3)
    eng.brain.c.min_minutes_between_wakes = 15
    eng.tick()  # first look, nothing to go on: the cheap 5 minute cache
    assert fake.requests[0]["system_blocks"][0]["cache_control"] == {"type": "ephemeral"}
    clock.t += 20 * 60
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()  # checking again 20 minutes later: keep it for an hour
    assert fake.requests[1]["system_blocks"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    clock.t += 5 * 3600
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()  # hours since the last check and hours until the next: back to 5 minutes
    assert fake.requests[2]["system_blocks"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Hyperliquid" in fake.requests[2]["system"]  # the prompt itself is unchanged


def test_cache_reads_and_one_hour_writes_are_priced():
    from types import SimpleNamespace
    from bot.claude_brain import usage_cost
    u = SimpleNamespace(input_tokens=1000, output_tokens=0, cache_read_input_tokens=5000, cache_creation_input_tokens=2000,
                        cache_creation=SimpleNamespace(ephemeral_1h_input_tokens=2000, ephemeral_5m_input_tokens=0),
                        server_tool_use=None)
    # Opus 5.5: $4 input, $0.20 cache read, 1h write = 2x input
    assert usage_cost(u, "claude-opus-5-5") == pytest.approx((1000 * 4 + 5000 * 0.20 + 2000 * 8) / 1e6)


def test_log_shows_cache_share():
    from tests.test_multi import actions, make_multi
    reply = tool_reply("submit_decisions", actions())
    reply.usage.cache_read_input_tokens, reply.usage.input_tokens = 6000, 2000
    eng, fake, market, clock = make_multi([reply])
    eng.tick()
    assert any("75% of the prompt from cache" in e["message"] for e in eng.store.events())
