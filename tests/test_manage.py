"""Trade management (take-profit, breakeven, trailing, time stop), portfolio risk limits
and the exposure view Claude gets."""

import pytest

from tests.test_bot import H
from tests.test_claude import tool_reply
from tests.test_multi import actions, make_multi, multi_send

DAY = 86400


def managed(**kw):
    g = multi_send()
    g.manage_trades, g.max_risk_per_trade, g.max_total_risk = True, 0.25, 0.0
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def step(eng, clock, market, coin, px, minutes=1):
    market.px[coin] = px
    clock.t += minutes * 60
    eng.tick()


def test_breakeven_then_take_half_then_trail_then_stop_out():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 40, 19.0, None)))], guardrails=managed())
    eng.store.set("claude_next_check_ts", clock.t + 99 * H)
    eng.tick()
    pos = eng.positions["SOL"]
    entry = pos["entry_px"]
    assert pos["target"] == pytest.approx(entry + 2 * (entry - 19.0))
    eng.store.set("claude_next_check_ts", clock.t + 99 * H)
    step(eng, clock, market, "SOL", entry + 1.05 * (entry - 19.0))
    assert eng.positions["SOL"]["stop"] == pytest.approx(entry) and eng.positions["SOL"]["be_done"]
    qty = eng.positions["SOL"]["qty"]
    step(eng, clock, market, "SOL", pos["target"] + 0.01)
    assert eng.positions["SOL"]["qty"] == pytest.approx(qty / 2, rel=0.01) and eng.positions["SOL"]["half_taken"]
    step(eng, clock, market, "SOL", 24.0)
    assert eng.positions["SOL"]["stop"] == pytest.approx(24.0 - (entry - 19.0))
    step(eng, clock, market, "SOL", 22.5)
    assert "SOL" not in eng.positions
    rec = eng.store.get("trade_records")[0]
    assert rec["pnl"] > 0 and rec["r"] > 1


def test_time_stop_closes_a_trade_going_nowhere():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 30, 19.0, None)))], guardrails=managed())
    eng.tick()
    eng.store.set("claude_next_check_ts", clock.t + 999 * H)
    step(eng, clock, market, "SOL", 20.2, minutes=60 * 119)
    assert "SOL" in eng.positions
    step(eng, clock, market, "SOL", 20.2, minutes=120)
    assert "SOL" not in eng.positions
    assert "Time stop" in eng.store.trades()[0]["reason"]


def test_claude_can_set_or_remove_a_target():
    a = actions(("long", "SOL", 30, 19.0, None))
    a["actions"][0]["target_price"] = 25.0
    b = actions()
    b["actions"] = [{"action": "set_target", "coin": "SOL", "position_pct": None, "close_pct": None, "stop_price": None,
                     "setup": None, "target_price": None}]
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", a), tool_reply("submit_decisions", b)], guardrails=managed())
    eng.tick()
    assert eng.positions["SOL"]["target"] == 25.0
    clock.t += 2 * H
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()
    assert eng.positions["SOL"]["target"] is None
    assert "TARGET SOL none" in eng.store.decisions(1, kind="decision")[0]["action"]


def test_portfolio_risk_cap_trims_new_trades():
    g = managed(max_risk_per_trade=0.02, max_total_risk=0.03)
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(
        ("long", "SOL", 40, 19.0, None), ("long", "BTC", 40, 950.0, None)))], guardrails=g)
    eng.tick()
    equity = eng.equity()
    assert eng.open_risk() <= equity * 0.03 * 1.03  # fills a touch worse than the mid
    sol_risk = eng.positions["SOL"]["qty"] * (eng.positions["SOL"]["entry_px"] - 19.0)
    assert sol_risk == pytest.approx(equity * 0.02, rel=0.05)
    assert any("across all open trades" in e["message"] for e in eng.store.events())


def test_breakeven_stop_frees_risk_room():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 40, 19.0, None)))],
                                          guardrails=managed(max_risk_per_trade=0.02, max_total_risk=0.10))
    eng.tick()
    assert eng.open_risk() > 0
    eng.store.set("claude_next_check_ts", clock.t + 99 * H)
    step(eng, clock, market, "SOL", 21.5)
    assert eng.open_risk() == pytest.approx(0, abs=0.01)


def test_context_shows_portfolio_and_trade_plan():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 30, 19.0, None), ("short", "ETH", 20, 105.0, None))),
                                           tool_reply("submit_decisions", actions())], guardrails=managed())
    eng.tick()
    clock.t += 2 * H
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()
    ctx = fake.requests[1]["messages"][0]["content"]
    assert "- Portfolio:" in ctx and "Behaves like" in ctx and "Open risk if every stop is hit" in ctx
    assert "target" in ctx and "time stop in" in ctx
    assert "Trade management runs automatically" in fake.requests[1]["system"]


def test_movement_stats_recorded_and_summarised():
    from bot import learning
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 40, 19.0, None)))], guardrails=managed())
    eng.tick()
    eng.store.set("claude_next_check_ts", clock.t + 999 * H)
    entry = eng.positions["SOL"]["entry_px"]
    step(eng, clock, market, "SOL", 19.4)   # goes against first
    step(eng, clock, market, "SOL", 20.9)   # then up nearly 1R
    step(eng, clock, market, "SOL", 18.9)   # and stops out
    rec = eng.store.get("trade_records")[0]
    assert rec["mae_r"] > 1 and rec["mfe_r"] == pytest.approx((20.9 - entry) / (entry - 19.0), rel=0.01)
    recs = [dict(rec, r=1.5, mfe_r=2.5, mae_r=0.9), dict(rec, r=-1.0, mfe_r=1.2, mae_r=1.0), rec]
    eng.store.set("trade_records", recs)
    s = learning.stats(eng.store)["movement"]
    assert s["n"] == 3 and s["winners_nearly_stopped"] == 1 and s["losers_were_up_1r"] == 1
    assert "How your 3 measured trades moved" in learning.scorecard(eng.store)


def test_scanner_marks_firing_setups_and_claude_sees_the_matching_research():
    from tests.test_multi import MultiMarket
    market = MultiMarket()
    market.series["SOL"] = [20.0] * 250 + [20 + i * 0.05 for i in range(1, 30)] + [24.0]
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())], market=market)
    eng.tick()
    sol = next(f for f in eng.brain._scan[1] if f["coin"] == "SOL")
    assert ("breakout", 1) in sol["setups"]
    ctx = fake.requests[0]["messages"][0]["content"]
    assert "## Playbook setups firing now" in ctx and "SOL (" in ctx and "breakout long is firing now. Over 7 years:" in ctx
    sol_row = next(f for f in eng.summary()["brain"]["scanner"] if f["coin"] == "SOL")
    assert sol_row["setups"][0].startswith("breakout long ★ +")


# Outside the charts

class FakeHTTP:
    def __init__(self, payloads):
        self.payloads, self.calls = payloads, []

    def get(self, url):
        from types import SimpleNamespace
        self.calls.append(url)
        data = self.payloads[next(k for k in self.payloads if k in url)]
        if isinstance(data, Exception):
            raise data
        return SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)


def calendar(now):
    from datetime import datetime, timezone
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return [{"title": "CPI m/m", "country": "USD", "date": iso(now + 30 * 60), "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
            {"title": "Retail Sales", "country": "USD", "date": iso(now + 26 * 3600), "impact": "Medium", "forecast": "", "previous": ""},
            {"title": "BOJ minutes", "country": "JPY", "date": iso(now + 3600), "impact": "High", "forecast": "", "previous": ""}]


def test_macro_feeds_parse_and_cache():
    from bot.macro import Macro, upcoming_text
    now = 1_800_000_000
    http = FakeHTTP({"ff_calendar": calendar(now), "coingecko": {"data": {"market_cap_percentage": {"btc": 57.3}, "market_cap_change_percentage_24h_usd": -1.2}}})
    m = Macro(client=http, clock=lambda: now)
    ev = m.events()
    assert [e["title"] for e in ev] == ["CPI m/m", "Retail Sales"]
    m.events()
    assert len(http.calls) == 1  # cached
    assert m.btc_dominance() == (57.3, -1.2)
    text = upcoming_text(ev, now)
    assert "CPI m/m (high impact) in 30 min, forecast 0.3%, previous 0.2%" in text and "Retail Sales" in text


def test_big_us_release_wakes_claude_and_shows_in_context():
    from bot.macro import Macro
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())] * 3)
    eng.brain.macro = Macro(client=FakeHTTP({"ff_calendar": calendar(clock.t + 3600),
                                             "coingecko": {"data": {"market_cap_percentage": {"btc": 57.3}}}}), clock=clock)
    eng.brain.c.min_minutes_between_wakes = 15
    eng.tick()
    eng.store.set("claude_next_check_ts", clock.t + 99 * H)
    clock.t += 40 * 60  # CPI is now 50 minutes away: not yet
    eng.tick()
    assert len(fake.requests) == 1
    clock.t += 10 * 60
    eng.tick()
    assert len(fake.requests) == 2
    ctx = fake.requests[1]["messages"][0]["content"]
    assert "US economic news coming: CPI m/m in 40 minutes" in ctx
    assert "## Scheduled US economic news" in ctx and "BTC is 57.3% of the whole crypto market" in ctx


def test_open_interest_change_tracked_from_hourly_snapshots():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())] * 3)
    eng.tick()
    assert next(f for f in eng.brain._scan[1] if f["coin"] == "SOL").get("oi_24h") is None
    hist = eng.store.get("oi_history")
    hist["SOL"] = [[clock.t - 23 * H, 0.5]] + hist["SOL"]
    eng.store.set("oi_history", hist)
    clock.t += 2 * H
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()
    sol = next(f for f in eng.brain._scan[1] if f["coin"] == "SOL")
    assert sol["oi_24h"] == pytest.approx(100.0)
    assert "open interest 24h" in fake.requests[-1]["messages"][0]["content"]
