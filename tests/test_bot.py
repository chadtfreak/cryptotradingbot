import pytest
from fastapi.testclient import TestClient

from bot.backtest import run_backtest
from bot.broker import PaperBroker
from bot.config import CostSettings, Settings, StrategySettings, load_settings
from bot.engine import DEAD, PAUSED, RUNNING, STOPPED, Engine
from bot.indicators import atr, ema
from bot.market import Candle
from bot.store import Store
from bot.strategy import on_candle_close, position_size
from bot.web import create_app

H = 3600


def make_candles(closes, start=1_700_000_000, step=4 * H, spread=1.0):
    return [Candle(start + i * step, c, c + spread, c - spread, c, 1.0) for i, c in enumerate(closes)]


def cross_up_series():
    """Long downtrend then a sharp rally, so the fast EMA crosses above the slow on the last candle."""
    closes = [200 - i for i in range(60)]
    closes += [closes[-1] + 4 * i for i in range(1, 40)]
    s = StrategySettings(fast_ema=5, slow_ema=20)
    for n in range(61, len(closes) + 1):
        snap_now = on_candle_close(make_candles(closes[:n]), s, False, None)
        if snap_now.action == "buy":
            return closes[:n]
    raise AssertionError("series never crossed")


class FakeMarket:
    def __init__(self, closes, price=None):
        self._candles = make_candles(closes)
        self._price = price if price is not None else closes[-1]

    def set(self, closes=None, price=None):
        if closes is not None:
            self._candles = make_candles(closes)
        if price is not None:
            self._price = price

    def candles(self, interval):
        return self._candles

    def price(self):
        return self._price

    def usd_to_aud(self):
        return 1.5


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


def settings():
    s = Settings()
    s.strategy.fast_ema, s.strategy.slow_ema = 5, 20
    return s


def make_engine(closes, price=None):
    clock = Clock()
    market = FakeMarket(closes, price)
    engine = Engine(settings(), market, Store(":memory:"), clock=clock)
    return engine, market, clock


# Indicators

def test_ema_matches_hand_calc():
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(3.0)
    assert out[4] == pytest.approx(4.0)


def test_atr_constant_range():
    highs, lows, closes = [11] * 20, [9] * 20, [10] * 20
    assert atr(highs, lows, closes, 14)[-1] == pytest.approx(2.0)


# Strategy and sizing

def test_buy_on_cross_and_sell_on_flip():
    closes = cross_up_series()
    d = on_candle_close(make_candles(closes), settings().strategy, False, None)
    assert d.action == "buy" and d.stop < closes[-1]
    falling = closes + [closes[-1] - 6 * i for i in range(1, 30)]
    assert on_candle_close(make_candles(falling), settings().strategy, True, d.stop).action == "sell"


def test_stop_only_trails_up():
    closes = [100 + i for i in range(60)]
    d = on_candle_close(make_candles(closes), settings().strategy, True, 10_000.0)
    assert d.action == "hold" and d.stop == 10_000.0


def test_position_size_respects_risk_and_cash():
    s = StrategySettings(risk_per_trade=0.02, max_position_pct=0.95, min_trade_usd=10)
    # 2% risk with a 10% stop allows 20% of equity
    assert position_size(100, 100, 100, 90, s, 0.05) == pytest.approx(20)
    # tight stop would want more than we have, so capped by cash
    assert position_size(100, 100, 100, 99.9, s, 0.05) == pytest.approx(94.95)
    # too small to bother
    assert position_size(100, 100, 100, 50, s, 0.05) == 0


def test_paper_broker_round_trip_costs():
    b = PaperBroker(CostSettings(pool_fee_pct=0.05, slippage_pct=0.10, gas_per_swap_usd=0.05))
    buy = b.buy(50, 1000)
    sell = b.sell(buy.qty, 1000)
    lost = -(buy.cash_delta + sell.cash_delta)
    assert 0.2 < lost < 0.3  # about 0.3% round trip plus 10c gas on $50


# Engine

def test_engine_buys_on_cross_and_records_trade():
    engine, market, clock = make_engine(cross_up_series())
    engine.tick()
    assert engine.qty > 0
    assert engine.store.get("stop") is not None
    [t] = engine.store.trades()
    assert t["side"] == "buy" and t["usd_aud"] == 1.5
    assert engine.equity(market.price()) < 100  # paid fees


def test_engine_does_not_retrade_same_candle():
    engine, _, clock = make_engine(cross_up_series())
    engine.tick()
    clock.t += 60
    engine.tick()
    assert len(engine.store.trades()) == 1


def test_stop_hit_sells():
    engine, market, clock = make_engine(cross_up_series())
    engine.tick()
    stop = engine.store.get("stop")
    market.set(price=stop - 1)
    clock.t += 60
    engine.tick()
    assert engine.qty == 0
    sell = engine.store.trades()[0]
    assert sell["side"] == "sell" and sell["pnl"] < 0 and "Stop hit" in sell["reason"]


def test_running_costs_charged_hourly():
    engine, market, clock = make_engine([100] * 60)
    clock.t += 24 * H
    engine.tick()
    assert engine.cash == pytest.approx(100 - settings().survival.daily_cost_usd)
    assert engine.summary()["costs_paid"] == pytest.approx(settings().survival.daily_cost_usd)


def test_daily_loss_limit_pauses_then_resumes_next_day():
    engine, market, clock = make_engine(cross_up_series())
    engine.tick()
    price = market.price()
    # Drop enough to lose >5% of equity but stay above the stop
    engine.store.set("stop", 1.0)
    market.set(price=price * 0.5)
    clock.t += 60
    engine.tick()
    assert engine.status == PAUSED and engine.qty == 0
    clock.t += 86400
    engine.tick()
    assert engine.status == RUNNING


def test_dies_below_floor_and_stays_dead():
    engine, market, clock = make_engine([100] * 60)
    engine.store.set("cash", 40.0)
    clock.t += 60
    engine.tick()
    assert engine.status == DEAD
    engine.resume()
    assert engine.status == DEAD


def test_kill_switch_sells_and_resume():
    engine, market, clock = make_engine(cross_up_series())
    engine.tick()
    engine.kill()
    assert engine.status == STOPPED and engine.qty == 0
    engine.resume()
    assert engine.status == RUNNING


def test_tick_survives_network_error():
    engine, market, clock = make_engine([100] * 60)
    market.price = lambda: (_ for _ in ()).throw(RuntimeError("timeout"))
    engine.tick()
    assert engine.last_error == "timeout"
    assert engine.store.events()[0]["level"] == "error"


# Backtest

def test_backtest_runs_and_charges_costs():
    closes = cross_up_series() + [400] * 50
    result = run_backtest(make_candles(closes), settings())
    assert result.trades >= 0 and result.costs_paid > 0
    assert "Survived" in result.report()


def test_backtest_needs_enough_data():
    with pytest.raises(ValueError):
        run_backtest(make_candles([100] * 10), settings())


# Config and web

def test_repo_config_loads():
    s = load_settings("config.toml")
    assert s.bot.mode == "paper" and s.bot.starting_balance == 100


def test_config_rejects_live_mode(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[bot]\nmode = "live"\n')
    with pytest.raises(ValueError):
        load_settings(p)


def test_web_endpoints(monkeypatch):
    engine, market, clock = make_engine(cross_up_series())
    engine.tick()
    client = TestClient(create_app(engine, run_loop=False))
    assert client.get("/").status_code == 200
    assert client.get("/static/chart.umd.min.js").status_code == 200
    assert client.get("/api/status").json()["position"] is not None
    assert len(client.get("/api/trades").json()) == 1
    csv = client.get("/api/trades.csv").text
    assert "usdt_aud_rate" in csv and "buy" in csv
    assert client.post("/api/kill").json()["status"] == STOPPED


def test_web_password(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    engine, *_ = make_engine([100] * 60)
    client = TestClient(create_app(engine, run_loop=False))
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", auth=("me", "hunter2")).status_code == 200
