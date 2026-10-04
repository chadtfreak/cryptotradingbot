"""Proven edges: detection, auto-trading at standard size, and following skipped ones."""

import pytest

from bot import edge
from tests.test_bot import H
from tests.test_claude import tool_reply
from tests.test_manage import managed, step
from tests.test_multi import MultiMarket, actions, make_multi

CELLS = {"breakout|1": [1875, 0.09], "breakout|1|btc|BTC up": [1665, 0.10], "breakout|1|trend|choppy": [896, 0.12],
         "breakout|1|funding|normal": [1313, 0.12], "breakout|1|vol|low vol": [910, 0.16], "breakout|1|vol|high vol": [965, 0.02],
         "pullback|1": [4394, -0.08], "momentum|1": [2062, 0.08], "momentum|1|btc|BTC up": [1615, 0.11],
         "momentum|1|trend|trending": [602, 0.05], "momentum|1|funding|normal": [1396, 0.09], "momentum|1|vol|high vol": [1398, 0.04]}


def reg(**kw):
    base = {"btc": "BTC up", "trend": "choppy", "funding": "normal", "vol": "low vol"}
    return {**base, **kw}


def test_estimate_needs_a_real_edge_in_these_conditions():
    assert edge.estimate(CELLS, "breakout", 1, reg()) == pytest.approx((0.10 + 0.12 + 0.12 + 0.16) / 4)
    assert edge.estimate(CELLS, "pullback", 1, reg()) is None  # loses overall
    assert edge.estimate(CELLS, "momentum", 1, reg(trend="trending", vol="high vol")) is None  # +0.07R: below the bar
    bad = {**CELLS, "breakout|1|funding|longs crowded": [362, -0.04]}
    assert edge.estimate(bad, "breakout", 1, reg(funding="longs crowded")) is None  # a losing condition vetoes it


def breakout_market():
    market = MultiMarket()
    market.series["SOL"] = [20.0] * 250 + [20 + i * 0.05 for i in range(1, 30)] + [24.0]
    return market


def auto_engine(auto=True, replies=None):
    eng, fake, market, clock = make_multi(replies or [tool_reply("submit_decisions", actions())] * 4, market=breakout_market(),
                                          guardrails=managed(max_risk_per_trade=0.02, max_total_risk=0.10))
    eng.brain.c.auto_trade_proven = auto
    eng.brain._regime_cells = CELLS
    eng.deposit(clock.t, 900, "bigger bankroll")  # a 1% risk trade on $100 would be under the $10 minimum
    return eng, fake, market, clock


def test_proven_setup_is_auto_opened_at_standard_size_without_waking_claude():
    eng, fake, market, clock = auto_engine()
    eng.tick()
    assert "SOL" in eng.positions and fake.requests == []
    pos = eng.positions["SOL"]
    risk = pos["qty"] * (pos["entry_px"] - pos["stop"])
    assert risk == pytest.approx(eng.equity() * 0.01, rel=0.05)
    assert pos["target"] > pos["entry_px"] and pos["setup"] == "breakout"
    clock.t += 60
    eng.tick()  # same candle: not opened twice, and now Claude gets its first look
    assert len([t for t in eng.store.trades() if t["coin"] == "SOL"]) == 1
    ctx = fake.requests[0]["messages"][0]["content"]
    assert "## Opened by code since your last check" in ctx and "SOL breakout long" in ctx
    assert "PROVEN EDGE" in ctx and "Code opens them automatically" in fake.requests[0]["system"]
    assert "★" in next(f for f in eng.summary()["brain"]["scanner"] if f["coin"] == "SOL")["setups"][0]


def test_skipped_proven_setup_is_followed_and_scored():
    eng, fake, market, clock = auto_engine(auto=False)
    eng.tick()  # Claude holds
    assert "SOL" not in eng.positions
    clock.t += 60
    eng.store.set("claude_next_check_ts", clock.t + 99 * H)
    eng.tick()
    skipped = eng.store.get("skipped_edges")
    assert len(skipped) == 1 and skipped[0]["coin"] == "SOL" and skipped[0]["setup"] == "breakout"
    step(eng, clock, market, "SOL", skipped[0]["target"] + 0.1)
    res = eng.store.get("skipped_results")
    assert res and res[0]["r"] == pytest.approx(2.0)
    from bot import learning
    assert "Proven setups that weren't traded: 1 marked" in learning.scorecard(eng.store)


def test_prompt_no_longer_tells_claude_to_hold_by_default():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())])
    eng.tick()
    system = fake.requests[0]["system"]
    assert "placing a trade costs no extra thinking" in system
    assert 'Most wake-ups should end in "hold"' not in system and "Undertrading" in system
