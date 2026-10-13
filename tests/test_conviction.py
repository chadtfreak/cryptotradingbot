"""Conviction mode: study, then a few big leveraged bets sized by conviction and held on a thesis."""

import pytest

from tests.test_bot import H
from tests.test_claude import tool_reply
from tests.test_multi import actions, make_multi, multi_send


def conviction(**kw):
    g = multi_send()
    g.style, g.thesis_mode, g.min_conviction = "conviction", True, 7
    g.max_leverage, g.max_positions, g.max_risk_per_trade, g.max_total_risk = 3.0, 3, 0.10, 0.30
    g.manage_trades, g.drawdown_half_risk_pct, g.drawdown_pause_pct, g.max_btc_exposure = False, 0, 0, 0
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def bet(action, coin, stop, conv, target=None, review=48, why="Funding is crowded and the catalyst is next week."):
    return {"action": action, "coin": coin, "position_pct": None, "close_pct": None, "stop_price": stop, "setup": "other",
            "target_price": target, "conviction": conv, "thesis": why, "catalyst": "Token unlock on Friday", "review_hours": review}


def decide(*acts, hours=24):
    d = actions(hours=hours)
    d["actions"] = list(acts)
    return d


def engine(replies, **kw):
    eng, fake, market, clock = make_multi(replies, guardrails=conviction(**kw))
    eng.deposit(clock.t, 900, "testnet bankroll")
    return eng, fake, market, clock


def risk(eng, coin):
    p = eng.positions[coin]
    return abs(p["qty"]) * abs(p["entry_px"] - p["stop"])


def test_conviction_sets_the_size_and_low_conviction_is_refused():
    eng, fake, market, clock = engine([tool_reply("submit_decisions", decide(
        bet("long", "SOL", 19.0, 10), bet("short", "BTC", 1100.0, 7), bet("long", "ETH", 95.0, 5)))])  # leaves room under the 3x cap
    eng.tick()
    equity = eng.equity()
    assert risk(eng, "SOL") == pytest.approx(equity * 0.10, rel=0.05)
    assert risk(eng, "BTC") == pytest.approx(equity * 0.04, rel=0.05)
    assert "ETH" not in eng.positions
    assert any("Conviction 5/10 is below the 7" in e["message"] for e in eng.store.events())
    assert eng.positions["SOL"]["thesis"]["conviction"] == 10 and "Funding is crowded" in eng.positions["SOL"]["thesis"]["why"]
    assert "LONG SOL conviction 10/10" in eng.store.decisions(1, kind="decision")[0]["action"]


def test_leverage_up_to_three_times_equity():
    eng, fake, market, clock = engine([tool_reply("submit_decisions", decide(bet("long", "SOL", 19.6, 10)))])  # a 2% stop
    eng.tick()
    assert eng.exposure() > eng.equity() * 2.5  # wants 5x, capped at 3x
    assert eng.exposure() <= eng.equity() * 3.0 + 0.5


def test_only_stop_and_target_close_a_bet():
    eng, fake, market, clock = engine([tool_reply("submit_decisions", decide(bet("long", "SOL", 19.0, 9, target=24.0)))])
    eng.tick()
    eng.store.set("claude_next_check_ts", clock.t + 999 * H)
    eng.store.set("claude_last_research_ts", clock.t)
    stop = eng.positions["SOL"]["stop"]
    for p in (21.5, 20.2):  # up past 1R, then back: no breakeven move in conviction mode
        market.px["SOL"] = p
        clock.t += 60
        eng.tick()
    assert eng.positions["SOL"]["stop"] == stop
    clock.t += 7 * 24 * H  # no time stop either
    eng.store.set("claude_last_research_ts", clock.t)
    eng.tick()
    assert "SOL" in eng.positions
    market.px["SOL"] = 24.1
    clock.t += 60
    eng.tick()
    assert "SOL" not in eng.positions
    assert "Thesis target reached" in eng.store.trades()[0]["reason"]


def test_rhythm_deep_dives_then_checks_only_for_bets_held():
    replies = [tool_reply("submit_decisions", decide(hours=6))] * 3 + [tool_reply("submit_decisions", decide(bet("long", "SOL", 15.0, 8, review=12)))] \
        + [tool_reply("submit_decisions", decide(hours=6))] * 4
    eng, fake, market, clock = engine(replies)
    eng.brain.c.min_minutes_between_wakes = 30
    eng.tick()
    assert "Research: first deep dive" in fake.requests[0]["messages"][0]["content"]
    assert fake.requests[0]["output_config"] == {"effort": "high"} and "A DEEP DIVE" in fake.requests[0]["messages"][0]["content"]
    market.px["ETH"] *= 1.10  # a coin it doesn't hold moves: no wake
    clock.t += 2 * H
    eng.tick()
    assert len(fake.requests) == 1
    clock.t += 7 * H  # 9 hours with no bets: hunt again
    eng.tick()
    assert "Research: no bets on" in fake.requests[1]["messages"][0]["content"]
    clock.t += 9 * H
    eng.tick()  # third deep dive opens a bet with a 12 hour review
    clock.t += 9 * H
    eng.tick()
    assert "SOL" in eng.positions
    clock.t += 13 * H
    eng.tick()
    ctx = fake.requests[-1]["messages"][0]["content"]
    assert "Thesis review due for your SOL bet" in ctx and "A QUICK CHECK" in ctx
    assert fake.requests[-1]["output_config"] == {"effort": "medium"}
    assert "conviction 8/10" in ctx and "Thesis: Funding is crowded" in ctx


def test_prompt_describes_conviction_mode():
    eng, fake, market, clock = engine([tool_reply("submit_decisions", decide())])
    eng.tick()
    system = fake.requests[0]["system"]
    assert "with up to 3x leverage" in system and "never more total exposure than 3x your equity" in system
    assert "conviction 7 risks 4% of equity at the stop, rising to 10% at conviction 10" in system
    assert "Chris Camillo" in system and "Trade management runs automatically" not in system
    tool = next(t for t in fake.requests[0]["tools"] if t.get("name") == "submit_decisions")
    assert "conviction" in tool["input_schema"]["properties"]["actions"]["items"]["required"]


def test_real_money_is_held_to_one_times():
    from tests.test_live import make_live
    eng, venue, clock = make_live([tool_reply("submit_decision", actions())])
    eng.base_g = conviction()
    assert eng.g.max_leverage == 3.0
    venue.network = "mainnet"
    assert eng.g.max_leverage == 1.0
    calls = []
    venue.set_leverage = lambda lev, coin=None: calls.append(lev)
    eng._ensure_leverage("SOL", clock.t)
    assert calls == [1]
