"""Multi-coin trading, the scanner, Claude's career, the backtester and the practice run."""

import json
import math
from types import SimpleNamespace

import pytest

from bot.career import Career, LEVELS, month_bounds
from bot.claude_brain import ClaudeBrain
from bot.engine import Engine
from bot.market import Candle
from bot.practice import PracticeRun, grade, scenario_text, summarise
from bot.research import backtest_coin, simulate, stats
from bot.scanner import alerts, coin_features, scan
from bot.store import Store
from tests.test_bot import Clock, H, make_candles, settings
from tests.test_claude import FakeClaude, decision, full_send, tool_reply

DAY = 86400


def wave(n, start=100.0, drift=0.002, amp=0.03, period=40):
    return [start * (1 + drift) ** i * (1 + amp * math.sin(i / period * 2 * math.pi)) for i in range(n)]


class MultiMarket:
    """Several coins with their own prices and candles."""

    def __init__(self, prices=None, volumes=None):
        self.px = prices or {"ETH": 100.0, "SOL": 20.0, "BTC": 1000.0, "MEME": 1.0}
        self.vol = volumes or {"ETH": 900e6, "SOL": 300e6, "BTC": 2000e6, "MEME": 5e6}
        self.funding = {}
        self.series = {c: wave(300, p / 1.8) for c, p in self.px.items()}
        self.last_funding = 0.0

    def mids(self):
        return dict(self.px)

    def price(self, coin=None):
        return self.px[coin or "ETH"]

    def candles(self, interval, coin=None, count=300, pair=None):
        coin = "BTC" if pair == "XBTUSDT" else (coin or "ETH")
        return make_candles(self.series[coin][-count:], step=interval * 60)

    def universe(self):
        return [{"coin": c, "volume": v, "funding": self.funding.get(c, 0.0), "open_interest": 1.0, "mark": self.px[c],
                 "prev_day": self.px[c], "max_leverage": 10, "sz_decimals": 2} for c, v in sorted(self.vol.items(), key=lambda x: -x[1])]

    def liquid(self, min_volume):
        return [c for c, v in self.vol.items() if v >= min_volume]

    def perp_context(self, coin=None):
        return {"funding": 0.0, "open_interest": 1.0, "mark": self.px[coin or "ETH"], "volume_24h": self.vol[coin or "ETH"]}

    def funding_rate(self, coin=None):
        return self.funding.get(coin or "ETH", 0.0)

    def usd_to_aud(self):
        return 1.5

    def fear_greed(self):
        return (50, "Neutral")


def multi_send():
    g = full_send()
    g.max_positions, g.min_volume_usd = 3, 50e6
    return g


def make_multi(replies, guardrails=None, market=None, rival=None):
    s = settings()
    clock = Clock()
    fake = FakeClaude(replies)
    brain = ClaudeBrain(s, client_factory=lambda k: fake, clock=clock)
    brain.rival = rival
    market = market or MultiMarket()
    eng = Engine(s, market, Store(":memory:"), clock=clock, brain=brain, guardrails=guardrails or multi_send())
    eng.store.set("anthropic_api_key", "sk-ant-test")
    return eng, fake, market, clock


def actions(*acts, hours=24):
    return {"actions": [{"action": a, "coin": c, "position_pct": pct, "close_pct": cp, "stop_price": stop, "setup": "breakout"}
                        for a, c, pct, stop, cp in acts],
            "confidence": "medium", "reasoning": "Multi test.", "journal": "j", "next_check_hours": hours}


def wake(eng, clock, hours=2):
    clock.t += hours * H
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()


# Multi-coin engine

def test_two_coins_long_and_short_with_combined_equity():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(
        ("long", "SOL", 40, 18.0, None), ("short", "ETH", 30, 110.0, None)))])
    eng.tick()
    assert set(eng.positions) == {"SOL", "ETH"}
    assert eng.positions["SOL"]["qty"] > 0 and eng.positions["ETH"]["qty"] < 0
    before = eng.equity()
    market.px["SOL"] = 22.0  # +10% on the long
    market.px["ETH"] = 90.0  # -10% helps the short
    eng.prices = market.mids()
    assert eng.equity() - before == pytest.approx(0.1 * 40 + 0.1 * 30, rel=0.05)
    trades = eng.store.trades()
    assert {t["coin"] for t in trades} == {"SOL", "ETH"}


def test_illiquid_coin_rejected():
    eng, *_ = make_multi([tool_reply("submit_decisions", actions(("long", "MEME", 30, 0.9, None)))])
    eng.tick()
    assert eng.positions == {}
    assert any("isn't on the list of liquid coins" in e["message"] for e in eng.store.events())


def test_max_positions_enforced():
    g = multi_send()
    g.max_positions = 2
    eng, *_ = make_multi([tool_reply("submit_decisions", actions(
        ("long", "SOL", 20, 18.0, None), ("long", "BTC", 20, 950.0, None), ("short", "ETH", 20, 110.0, None)))], guardrails=g)
    eng.tick()
    assert len(eng.positions) == 2
    assert any("most allowed" in e["message"] for e in eng.store.events())


def test_total_exposure_capped_across_coins():
    eng, *_ = make_multi([tool_reply("submit_decisions", actions(
        ("long", "SOL", 60, 10.0, None), ("long", "BTC", 60, 500.0, None)))])
    eng.tick()
    assert eng.exposure() <= eng.equity() * 1.0 + 0.01


def test_per_coin_stops():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions(
        ("long", "SOL", 30, 19.0, None), ("long", "BTC", 30, 950.0, None)))])
    eng.tick()
    market.px["SOL"] = 18.5  # SOL stop hit, BTC fine
    clock.t += 60
    eng.tick()
    assert "SOL" not in eng.positions and "BTC" in eng.positions
    assert "Stop hit: SOL" in eng.store.trades()[0]["reason"]


def test_kill_closes_every_position():
    eng, *_ = make_multi([tool_reply("submit_decisions", actions(
        ("long", "SOL", 30, 18.0, None), ("short", "ETH", 30, 110.0, None)))])
    eng.tick()
    eng.kill()
    assert eng.positions == {}


def test_legacy_single_position_database_migrates():
    s = settings()
    store = Store(":memory:")
    for k, v in {"cash": 50.0, "qty": 0.5, "cost_basis": 50.0, "stop": 95.0, "status": "running", "started_at": 1,
                 "last_cost_ts": 1, "day": "2027-01-15", "day_start_equity": 100.0}.items():
        store.set(k, v)
    eng = Engine(s, MultiMarket(), store, clock=Clock())
    assert eng.position("ETH") == {"qty": 0.5, "cost_basis": 50.0, "stop": 95.0}
    assert eng.qty == 0.5


def test_old_trades_table_gets_coin_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE trades (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
        price REAL NOT NULL, notional REAL NOT NULL, fee REAL NOT NULL, gas REAL NOT NULL, pnl REAL, usd_aud REAL, reason TEXT NOT NULL);
        INSERT INTO trades (ts, side, qty, price, notional, fee, gas, reason) VALUES (1, 'buy', 1, 1, 1, 0, 0, 'old');""")
    con.commit()
    con.close()
    assert Store(str(path)).trades()[0]["coin"] == "ETH"


# Scanner

def test_scanner_flags_breakout_and_ranks_it_first():
    closes = [100.0] * 200 + [100 + i for i in range(1, 7)] + [120.0]
    breakout = make_candles(closes)
    flat = make_candles([100.0 + (i % 2) for i in range(207)])
    f_up = coin_features("UP", breakout, {"funding": 0.0, "volume": 100e6})
    f_flat = coin_features("FLAT", flat, {"funding": 0.0, "volume": 100e6})
    assert f_up["breakout"] and not f_flat["breakout"]
    assert f_up["score"] > f_flat["score"]
    assert any(k == "breakout" for k, _ in alerts(f_up))


def test_scan_covers_liquid_coins_and_held():
    m = MultiMarket()
    feats = scan(m, 50e6, held=["MEME"])
    assert {f["coin"] for f in feats} == {"ETH", "SOL", "BTC", "MEME"}


def test_scanner_alert_wakes_claude_once_per_day():
    market = MultiMarket()
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())] * 3, market=market)
    eng.tick()  # first look
    market.series["SOL"] = [20.0] * 250 + [20 + i * 0.5 for i in range(1, 7)] + [30.0]
    eng.brain._scan = (0.0, [])
    clock.t += 2 * H
    eng.store.set("claude_next_check_ts", clock.t + 10 * H)
    eng.tick()
    assert "Scanner: SOL closed above its 20-day high" in fake.requests[-1]["messages"][0]["content"]
    clock.t += 2 * H
    eng.tick()
    assert len(fake.requests) == 2  # the same alert doesn't wake it again today


def test_context_shows_scanner_and_positions():
    eng, fake, *_ = make_multi([tool_reply("submit_decisions", actions(("long", "SOL", 30, 18.0, None)))] * 2)
    eng.tick()
    wake = fake  # noqa
    ctx = fake.requests[0]["messages"][0]["content"]
    assert "## Scanner" in ctx and "SOL |" in ctx and "## Your career" in ctx
    assert "# Your playbook" in fake.requests[0]["system"]
    assert "Backtested setup statistics" in fake.requests[0]["system"]


# Career

def equity_series(store, start, days, start_eq, end_eq, start_px=100.0, end_px=100.0):
    for k in range(days * 24 + 1):
        frac = k / (days * 24)
        store.add_equity(start + k * 3600, start_eq + (end_eq - start_eq) * frac, 0, 0, start_px + (end_px - start_px) * frac)


def career_engine(now, ret_me, ret_rival, hold_px_end=100.0):
    s = settings()
    clock = Clock(now)
    rival = Engine(s, MultiMarket(), Store(":memory:"), clock=clock)
    eng, fake, market, _ = make_multi([], rival=rival)
    eng.clock = eng.brain.clock = clock
    prev_start, prev_end = month_bounds(month_bounds(now)[0] - 1)
    equity_series(eng.store, prev_start, 28, 100, 100 * (1 + ret_me / 100), 100, hold_px_end)
    equity_series(rival.store, prev_start, 28, 100, 100 * (1 + ret_rival / 100))
    return eng, rival


def test_beating_both_rivals_earns_full_allowance():
    now = month_bounds(1_800_000_000)[0] + 3 * DAY
    eng, rival = career_engine(now, ret_me=8, ret_rival=2, hold_px_end=104)
    c = Career(15.0)
    c.roll_month(eng, rival, now)
    a = c.allowance(eng.store, now)
    assert a["amount"] == 15.0 and a["smart"]


def test_beating_neither_cuts_allowance_and_model():
    now = month_bounds(1_800_000_000)[0] + 3 * DAY
    eng, rival = career_engine(now, ret_me=-3, ret_rival=2, hold_px_end=104)
    c = Career(15.0)
    c.roll_month(eng, rival, now)
    a = c.allowance(eng.store, now)
    assert a["amount"] == 6.0 and not a["smart"]
    tier = eng.brain.tier(eng, now)
    assert tier["budget"] == 6.0 and tier["name"] != "sharp"


def test_two_losing_months_mean_probation():
    now = month_bounds(1_800_000_000)[0] + 3 * DAY
    eng, rival = career_engine(now, ret_me=-3, ret_rival=2)
    eng.store.set("losing_streak", 1)
    Career(15.0).roll_month(eng, rival, now)
    assert eng.store.get("probation") is True
    assert eng.g.max_positions == 1 and eng.g.max_risk_per_trade == pytest.approx(multi_send().max_risk_per_trade / 2)


def test_promotion_offer_and_approval_adds_bankroll():
    now = 1_800_000_000
    born = now - 40 * DAY
    rival = Engine(settings(), MultiMarket(), Store(":memory:"), clock=Clock(born))
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())], rival=rival)
    clock.t = born
    eng.store.set("started_at", born)
    for e in (eng, rival):  # birth deposits happened long before the window
        e.store.conn.execute("UPDATE ledger SET ts = ?", (born,))
    eng.tick()
    clock.t = now
    start = now - 31 * DAY
    equity_series(eng.store, start, 31, 100, 115)
    equity_series(rival.store, start, 31, 100, 101)
    for k in range(6):
        eng.store.add_trade(now - k * DAY, SimpleNamespace(side="sell", qty=1, price=1, notional=1, fee=0, gas=0), 1.0, None, "win", coin="SOL")
    c = eng.brain.career
    c.check_promotion(eng, rival, now)
    offer = eng.store.get("promotion_offer")
    assert offer and offer["name"] == LEVELS[1][0]
    before = eng.contributed
    c.approve(eng, now)
    assert eng.contributed == before + LEVELS[1][1]
    assert eng.store.get("career_level") == 1 and eng.store.get("promotion_offer") is None


def test_promotion_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from bot.web import create_app
    eng, *_ = make_multi([tool_reply("submit_decisions", actions())])
    eng.tick()
    client = TestClient(create_app({"claude": eng}, run_loop=False))
    assert client.post("/api/claude/promotion", json={"approve": True}).status_code == 400
    eng.store.set("promotion_offer", {"level": 1, "name": "Trader", "amount": 100, "since": 1, "stats": {}})
    assert client.post("/api/claude/promotion", json={"approve": True}).json()["starting_balance"] == pytest.approx(200)


# Backtester

def test_simulate_stop_target_and_fees():
    up = make_candles([100, 100, 103.1, 104])  # hits a +3 target
    down = make_candles([100, 100, 98.4, 97])  # hits a -1.5 stop
    a = [1.0] * 4
    assert simulate(up, 0, 1, 1.5, 3.0, a) == pytest.approx(2.0 - 0.0013 * 100 / 1.5, rel=0.01)
    assert simulate(down, 0, 1, 1.5, 3.0, a) == pytest.approx(-1.0 - 0.0013 * 100 / 1.5, rel=0.01)


def test_backtest_produces_trades_on_trending_data():
    trades = backtest_coin(make_candles(wave(800, 50, drift=0.003, amp=0.06, period=60)))
    assert trades and {t[0] for t in trades} <= {"breakout", "pullback", "range_fade", "failed_breakout", "momentum"}
    s = stats([t[2] for t in trades])
    assert s["n"] == len(trades) and 0 <= s["win"] <= 100


# Practice run

def test_scenario_is_anonymised():
    c = make_candles(wave(400, 2500))
    text = scenario_text(c, 300, None)
    assert "Coin X" in text and "2500" not in text and "ETH" not in text


def test_grade_long_hits_target():
    c = make_candles([100] * 10 + [100, 101, 103, 104] + [104] * 30)
    g = grade(c, 10, {"action": "long", "stop_pct": 2, "target_pct": 3})
    assert g["taken"] and g["exit"] == "target" and g["r"] > 1.4


def test_grade_pass_records_missed_move():
    c = make_candles([100] * 10 + [100, 105, 112] + [112] * 30)
    g = grade(c, 10, {"action": "pass", "stop_pct": None, "target_pct": None})
    assert not g["taken"] and g["move_up_pct"] > 10


def test_practice_run_end_to_end():
    trade = {"action": "long", "setup": "breakout", "stop_pct": 3, "target_pct": 6, "win_probability": 55, "reasoning": "r"}
    review = {"summary": "Learned a lot.", "lessons": "- Trust breakouts."}
    replies = [tool_reply("submit_practice_trade", trade, model="claude-opus-5-5")] * 12 + [tool_reply("submit_practice_review", review)]
    fake = FakeClaude(replies)
    brain = ClaudeBrain(settings(), client_factory=lambda k: fake)
    store = Store(":memory:")
    market = MultiMarket()
    market.series = {c: wave(1200, 100, amp=0.05) for c in market.series}
    run = PracticeRun(brain, store, "sk-ant-x", market, scenarios=12, seed=1, workers=2)
    run.status(state="running")
    run.run()
    st = store.get("practice_status")
    assert st["state"] == "done" and st["done"] == 12 and st["cost"] > 0
    assert store.get("practice_lessons") == "- Trust breakouts."
    report = store.get("practice_report")
    assert report["stats"]["taken"] == 12 and "calibration" in report["stats"]
    assert "Coin X" in fake.requests[0]["messages"][0]["content"]


def test_practice_lessons_reach_the_live_prompt():
    eng, fake, *_ = make_multi([tool_reply("submit_decisions", actions())])
    eng.store.set("practice_lessons", "- Never chase.")
    eng.tick()
    assert "- Never chase." in fake.requests[0]["system"]


def test_summarise_calibration():
    rs = [{"taken": True, "r": 2.0, "won": True, "win_probability": 70, "setup": "breakout"},
          {"taken": True, "r": -1.0, "won": False, "win_probability": 70, "setup": "breakout"},
          {"taken": False, "move_up_pct": 12, "move_down_pct": 1, "end_pct": 10}]
    s = summarise(rs)
    assert s["taken"] == 2 and s["calibration"]["over 60%"]["actual"] == 50 and s["passed_on_10pct_moves"] == 1


# Exchange hiccups

def test_brief_502_is_retried_quietly_then_reported(monkeypatch):
    import httpx
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())] * 3)
    eng.tick()
    req = httpx.Request("POST", "https://api.hyperliquid.xyz/info")
    err = httpx.HTTPStatusError("502", request=req, response=httpx.Response(502, request=req))

    def boom():
        raise err
    monkeypatch.setattr(market, "mids", boom)
    eng.tick(); eng.tick()
    assert eng.last_error is None and not any(e["level"] in ("error", "warning") for e in eng.store.events())
    eng.tick()
    assert "Can't reach Hyperliquid" in eng.last_error
    monkeypatch.undo()
    eng.tick()
    assert eng.last_error is None and "Connection is back" in eng.store.events()[0]["message"]


def test_market_post_retries_server_errors(monkeypatch):
    import httpx
    from bot.market import HyperliquidMarket
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(502) if len(calls) < 3 else httpx.Response(200, json={"ETH": "100"})
    m = HyperliquidMarket()
    m.client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("bot.market.time.sleep", lambda s: None)
    assert m.mids() == {"ETH": 100.0} and len(calls) == 3


# Long history research

def test_squeeze_fade_needs_funding():
    from bot.indicators import atr as atr_, ema as ema_, rsi as rsi_
    from bot.research import signals
    closes = [100.0] * 130 + [100 + i * 0.8 for i in range(1, 15)]
    c = make_candles(closes, spread=0.3)
    cl = [x.close for x in c]
    f, s, r = ema_(cl, 20), ema_(cl, 50), rsi_(cl)
    a = atr_([x.high for x in c], [x.low for x in c], cl, 14)
    i = len(c) - 1
    assert not any(k == "squeeze_fade" for k, *_ in signals(c, i, f, s, a, r))
    assert ("squeeze_fade", -1, 1.5, 3.0) in signals(c, i, f, s, a, r, funding=80.0)


def test_run_long_splits_by_market_mood():
    from bot.research import run_long
    data = {coin: {"candles": make_candles(wave(1500, start, amp=0.08, period=60 + k * 7)),
                   "funding": [(1_700_000_000 + j * 8 * 3600, (j % 9 - 3) * 12.0) for j in range(2000)]}
            for k, (coin, start) in enumerate([("BTC", 1000.0), ("ETH", 100.0), ("SOL", 20.0)])}
    text = run_long(data)
    assert "3 major coins" in text and "When BTC's 4h trend is up or down" in text
    assert "By funding" in text and "trust the numbers" in text


def test_funding_lookup_uses_latest_published_rate():
    from bot.history import _funding_lookup
    at = _funding_lookup([(100, 10.0), (200, -5.0)])
    assert at(50) is None and at(150) == 10.0 and at(200) == -5.0
