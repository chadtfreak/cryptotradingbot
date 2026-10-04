"""Drawdown risk scaling, the BTC bet cap, the go-live checklist and the weekly report."""

import pytest

from tests.test_bot import H
from tests.test_claude import tool_reply
from tests.test_manage import managed, step
from tests.test_multi import MultiMarket, actions, make_multi

DAY = 86400


def pro(**kw):
    return managed(drawdown_half_risk_pct=10, drawdown_pause_pct=20, drawdown_pause_hours=24, **kw)


def test_drawdown_halves_risk_then_pauses_then_recovers():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 60, 10.0, None)))] + [tool_reply("submit_decisions", actions())] * 3,
                                          guardrails=pro(max_risk_per_trade=0.6))
    eng.tick()
    full = eng.g.max_risk_per_trade
    eng.store.set("claude_next_check_ts", clock.t + 999 * H)
    step(eng, clock, market, "SOL", 16.0)  # about -12% on equity
    assert eng.store.get("drawdown") > 0.10 and eng.g.max_risk_per_trade == pytest.approx(full / 2)
    step(eng, clock, market, "SOL", 12.0)  # past -20%
    assert eng.store.get("dd_pause_until") > clock.t
    assert any("No new trades for 24 hours" in e["message"] for e in eng.store.events())
    eng.apply(__import__("bot.strategy", fromlist=["Decision"]).Decision("buy", "test", 950.0, size_usd=10, coin="BTC"), clock.t)
    assert "BTC" not in eng.positions and "no new trades for another" in eng.store.events()[0]["message"]
    step(eng, clock, market, "SOL", 30.0)  # back above the old high
    assert eng.store.get("drawdown") == 0 and eng.g.max_risk_per_trade == pytest.approx(full)
    assert eng.store.get("max_drawdown") > 0.2


def test_top_up_doesnt_count_as_a_gain_or_a_drawdown():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())], guardrails=pro())
    eng.tick()
    eng.top_up(clock.t, 900)
    clock.t += 60
    eng.tick()
    assert eng.store.get("drawdown") == pytest.approx(0, abs=0.002)  # only a minute of hosting, not the top-up
    assert eng.store.get("nav_peak") == pytest.approx(1.0, abs=0.01)


def test_btc_bet_cap_trims_correlated_longs():
    market = MultiMarket()
    market.series = {c: [p * (1 + 0.01 * ((i % 7) - 3)) for i, p in enumerate(s)] for c, s in market.series.items()}  # move together
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(
        ("long", "SOL", 50, 15.0, None), ("long", "ETH", 50, 70.0, None)))], market=market, guardrails=managed(max_btc_exposure=0.6))
    eng.tick()
    assert eng.btc_exposure() <= eng.equity() * 0.6 * 1.02
    assert any("act like more than 60% of equity in BTC" in e["message"] for e in eng.store.events())


def test_go_live_checklist():
    from bot import golive
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())], guardrails=pro())
    eng.tick()
    g = golive.check(eng, None, clock.t)
    assert g["total"] == 5 and g["passed"] < 5
    eng.store.set("started_at", clock.t - 61 * DAY)
    eng.store.set("trade_records", [{"closed": clock.t, "r": 1, "pnl": 1}] * 50)
    eng.store.set("max_drawdown", 0.05)
    eng.deposit(clock.t, 0.0, "x")
    g = golive.check(eng, None, clock.t)
    names = {i["name"]: i["ok"] for i in g["items"]}
    assert names["Trading for 60+ days"] and names["50+ closed trades"] and names["Worst drop under 20%"]
    assert "Path to real money" in golive.context(g)


def test_weekly_report_built_once_on_monday():
    from bot import report
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 30, 19.0, None)))] + [tool_reply("submit_decisions", actions())] * 3,
                                          guardrails=pro())
    monday = report.week_start(clock.t) + 7 * DAY
    eng.store.set("started_at", monday - 5 * DAY)
    eng.tick()
    eng.store.set("trade_records", [{"coin": "SOL", "setup": "breakout", "closed": monday - DAY, "pnl": 2.5, "r": 1.2, "ts": 0},
                                    {"coin": "ETH", "setup": "momentum", "closed": monday - DAY, "pnl": -1.0, "r": -1.0}])
    clock.t = monday + 60
    eng.store.set("claude_next_check_ts", clock.t + 99 * H)
    eng.tick()
    reps = eng.store.get("weekly_reports")
    assert len(reps) == 1 and reps[0]["trades"] == 2 and reps[0]["win_rate"] == 50
    assert reps[0]["best"]["coin"] == "SOL" and "2 trades closed" in reps[0]["summary"]
    clock.t += 3600
    eng.tick()
    assert len(eng.store.get("weekly_reports")) == 1
    assert eng.summary()["brain"]["reports"][0]["by_setup"]["breakout"]["n"] == 1
