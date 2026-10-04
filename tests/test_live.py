"""Live trading tests against a simulated Hyperliquid account. Nothing touches the network."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bot.engine import Engine
from bot.hyperliquid import AccountState, LiveBroker, VenueError, check_credentials, round_price
from bot.store import Store
from bot.venues import switch_venue
from bot.web import create_app
from tests.test_bot import Clock, H, settings
from tests.test_claude import FakeClaude, MarketPlus, decision, full_send, tool_reply


class FakeVenue:
    """A tiny Hyperliquid account: USDC balance, one ETH perp position and stop orders."""

    network = "testnet"

    def __init__(self, balance=1000.0, px=100.0):
        self.balance, self.px = balance, px
        self.qty, self.entry = 0.0, 0.0
        self.stops: list[tuple[float, float]] = []
        self.reject = None
        self.orders = []

    def price(self):
        return self.px

    def state(self):
        value = self.balance + self.qty * (self.px - self.entry)
        return AccountState(value, {"ETH": (self.qty, self.entry)} if self.qty else {})

    def market(self, is_buy, qty, reduce_only, coin=None):
        if self.reject:
            raise VenueError(self.reject)
        qty = round(qty, 4)
        signed = qty if is_buy else -qty
        self.orders.append((is_buy, qty, reduce_only))
        if self.qty and (self.qty > 0) != (signed > 0):  # reducing
            closed = min(abs(signed), abs(self.qty))
            self.balance += (self.px - self.entry) * closed * (1 if self.qty > 0 else -1)
            self.qty += signed
        else:
            total = self.qty + signed
            self.entry = (self.entry * self.qty + self.px * signed) / total
            self.qty = total
        self.balance -= qty * self.px * 0.00045
        return qty, self.px

    leverage = 20

    def set_leverage(self, leverage=1, coin=None):
        self.leverage = leverage

    def cancel_stops(self, coin=None):
        self.stops = []

    def place_stop(self, qty, stop, coin=None):
        self.stops.append((qty, stop))
        return 1

    def fire_stop(self):
        qty, stop = self.stops[0]
        self.px = stop
        self.market(qty < 0, abs(qty), True)
        self.stops = []


def make_live(replies, venue=None, store=None):
    s = settings()
    clock = Clock()
    fake = FakeClaude(replies)
    from bot.claude_brain import ClaudeBrain
    brain = ClaudeBrain(s, client_factory=lambda k: fake, clock=clock)
    venue = venue or FakeVenue()
    eng = Engine(s, MarketPlus([100.0] * 80), store or Store(":memory:"), broker=LiveBroker(venue, s.costs),
                 clock=clock, brain=brain, guardrails=full_send())
    eng.store.set("anthropic_api_key", "sk-ant-test")
    return eng, venue, clock


def test_bankroll_is_100_even_with_1000_on_testnet():
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    assert eng.summary()["mode"] == "testnet"
    # 100 bankroll less the first thought
    assert 99.9 < eng.equity(venue.px) < 100


def test_long_places_real_order_and_exchange_stop():
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("long", pct=50, stop=95))])
    eng.tick()
    assert venue.orders[0][0] is True and venue.orders[0][2] is False
    assert venue.qty == pytest.approx(0.5, abs=0.001)
    assert venue.stops == [(pytest.approx(venue.qty), 95)]
    assert eng.store.get("exchange_stops") == {"ETH": True}
    # Next tick reads the truth from the exchange
    venue.px = 110
    clock.t += 60
    eng.tick()
    assert eng.equity(110) == pytest.approx(100 + 0.5 * 10, abs=0.2)


def test_exchange_stop_firing_is_picked_up():
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("short", pct=50, stop=105))])
    eng.tick()
    assert venue.qty < 0
    venue.fire_stop()  # happens on the exchange while the bot is busy or offline
    clock.t += 60
    eng.tick()
    assert eng.qty == 0 and eng.position() is None
    t = eng.store.trades()[0]
    assert t["pnl"] < 0 and "Closed on the exchange" in t["reason"]
    assert 96 < eng.equity(105) < 98  # lost about 5% of the $50 short, plus fees and thinking


def test_close_uses_reduce_only_and_clears_stop():
    eng, venue, clock = make_live([
        tool_reply("submit_decision", decision("long", pct=50, stop=95)),
        tool_reply("submit_decision", decision("close", close_pct=100)),
    ])
    eng.tick()
    clock.t += 2 * H
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()
    assert venue.qty == 0
    assert venue.orders[-1][2] is True  # reduce only
    assert venue.stops == []


def test_rejected_order_is_logged_not_fatal():
    venue = FakeVenue()
    venue.reject = "Insufficient margin"
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("long", pct=50, stop=95))], venue)
    eng.tick()
    assert eng.qty == 0 and eng.last_error is None
    assert any("rejected the trade" in e["message"] for e in eng.store.events())


def test_thinking_costs_come_off_live_equity():
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    clock.t += 24 * H
    eng.tick()
    s = eng.summary()
    assert s["equity"] == pytest.approx(100 - s["ai_paid"] - s["hosting_paid"], abs=0.01)


def test_dust_after_close_counts_as_flat():
    venue = FakeVenue()
    venue.qty, venue.entry = 0.00001, 100.0
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("hold"))], venue)
    eng.tick()
    assert eng.qty == 0


def test_round_price_rules():
    assert round_price(2714.8765, 4) == 2714.9
    assert round_price(98765.4321, 5) == 98765.0
    assert round_price(0.123456789, 0) == 0.12346


def test_check_credentials_rejects_bad_input_without_network():
    ok, msg, _ = check_credentials("testnet", "0x123", "0xabc")
    assert not ok and "42 characters" in msg
    ok, msg, _ = check_credentials("testnet", "0x" + "1" * 40, "not-a-key")
    assert not ok and "isn't valid" in msg


# Switching venue

def test_switch_venue_archives_and_keeps_lessons(tmp_path, monkeypatch):
    s = settings()
    s.claude.db_path = str(tmp_path / "claude.db")
    from bot.venues import build_claude
    old = build_claude(s, None)
    old.store.set("lessons", "- Be patient.")
    old.store.set("anthropic_api_key", "sk-ant-x")
    monkeypatch.setattr("bot.hyperliquid.HyperliquidVenue._connect", lambda self: None)
    new = switch_venue(s, old, {"name": "testnet", "account": "0x" + "1" * 40, "agent_key": "0x" + "2" * 64})
    assert old.retired
    assert new.live and new.broker.venue.network == "testnet"
    assert new.store.get("lessons") == "- Be patient."
    assert new.store.get("anthropic_api_key") == "sk-ant-x"
    assert any(p.name.startswith("claude-paper-") for p in tmp_path.iterdir())
    old.tick()  # a retired engine does nothing, even with its store closed


def test_switch_refused_with_open_position(tmp_path):
    s = settings()
    s.claude.db_path = str(tmp_path / "claude.db")
    from bot.venues import build_claude
    eng = build_claude(s, None)
    eng.store.set("positions", {"ETH": {"qty": 0.5, "cost_basis": 50.0, "stop": 90.0}})
    with pytest.raises(ValueError, match="Close Claude's position"):
        switch_venue(s, eng, {"name": "paper"})


def test_venue_endpoint_locks_mainnet_and_hides_key(monkeypatch):
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("hold"))])
    eng.store.set("venue", {"name": "testnet", "account": "0xabc", "agent_key": "0xSECRET"})
    eng.tick()
    client = TestClient(create_app({"claude": eng}, run_loop=False))
    r = client.post("/api/claude/venue", json={"venue": "mainnet", "account": "0x" + "1" * 40, "agent_key": "0x" + "2" * 64})
    assert r.status_code == 400 and "locked" in r.json()["detail"]
    body = client.get("/api/claude/status").text
    assert "0xSECRET" not in body and '"venue":"testnet"' in body


# Account types

class FakeInfo:
    def __init__(self, mode, perps_value, spot_usdc, positions=()):
        self.mode, self.perps_value, self.spot_usdc, self.positions = mode, perps_value, spot_usdc, positions

    def user_state(self, account):
        return {"marginSummary": {"accountValue": str(self.perps_value)},
                "assetPositions": [{"position": {"coin": c, "szi": str(q), "entryPx": str(e), "unrealizedPnl": str(u)}}
                                   for c, q, e, u in self.positions]}

    def spot_user_state(self, account):
        return {"balances": [{"coin": "USDC", "total": str(self.spot_usdc)}, {"coin": "HYPE", "total": "3"}]}

    def query_user_abstraction_state(self, account):
        return self.mode


def test_unified_account_reads_spot_usdc_plus_unrealised():
    from bot.hyperliquid import account_state
    info = FakeInfo("unifiedAccount", 0.0, 999.0, [("ETH", -0.02, 2700.0, 1.5)])
    st = account_state(info, "0xabc")
    assert st.account_value == pytest.approx(1000.5)
    assert st.qty == -0.02 and st.entry_price == 2700.0


def test_standard_account_reads_perps_value():
    from bot.hyperliquid import account_state
    info = FakeInfo("default", 250.0, 999.0)
    assert account_state(info, "0xabc").account_value == 250.0


def test_sets_exchange_leverage_to_1x_on_first_trade():
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("long", pct=30, stop=95))])
    eng.tick()
    assert venue.leverage == 1
    assert any("leverage for ETH to 1x" in e["message"] for e in eng.store.events())


def test_thin_testnet_book_skips_the_order_and_tells_claude():
    class ThinVenue(FakeVenue):
        def book_problem(self, coin, real_price=None):
            return "its order book is too thin (12.0% gap between buyers and sellers)"
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("buy", pct=40, stop=95.0)),
                                   tool_reply("submit_decision", decision("hold"))], venue=ThinVenue())
    eng.tick()
    assert venue.orders == [] and eng.positions == {}
    assert "too thin" in eng.store.get("untradeable")["ETH"]["why"]
    assert any("Can't buy ETH on this venue right now" in e["message"] for e in eng.store.events())
    clock.t += 3 * 3600
    eng.store.set("claude_next_check_ts", clock.t - 1)
    eng.tick()
    assert "## Can't trade on this venue right now" in eng.brain._client.requests[1]["messages"][0]["content"]


def test_book_problem_reads_the_order_book():
    from bot.hyperliquid import HyperliquidVenue

    class BookInfo:
        def __init__(self, bid, ask):
            self.book = {"levels": [[{"px": str(bid), "sz": "1"}], [{"px": str(ask), "sz": "1"}]]}

        def meta(self):
            return {"universe": [{"name": "PUMP", "szDecimals": 0}, {"name": "ZEC", "szDecimals": 2, "isDelisted": True}]}

        def l2_snapshot(self, coin):
            return self.book
    v = HyperliquidVenue("testnet", "0x" + "1" * 40, "0x" + "2" * 64)
    v._info = BookInfo(0.006403, 0.007187)
    v._connect = lambda: None
    assert "too thin" in v.book_problem("PUMP", 0.006285)
    v._info, v._sz = BookInfo(0.00628, 0.00629), None
    assert v.book_problem("PUMP", 0.006285) is None
    assert "away from the real market" in v.book_problem("PUMP", 0.0055)
    assert "isn't listed" in v.book_problem("ZEC")


# Bankroll top-ups

def test_top_up_uses_the_whole_testnet_account_and_moves_the_floor():
    from fastapi.testclient import TestClient
    from bot.web import create_app
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    assert eng.bankroll_base == 100 and eng.floor == 50
    room = eng.available_to_add()
    assert 899 < room <= 901  # a little over 900: thinking costs come off equity, not the account
    client = TestClient(create_app({"claude": eng}, run_loop=False))
    assert client.post("/api/claude/bankroll", json={"amount": 5000}).status_code == 400
    s = client.post("/api/claude/bankroll", json={"full": True}).json()
    assert s["bankroll_base"] == pytest.approx(100 + room, abs=0.02) and s["floor"] == pytest.approx(s["bankroll_base"] / 2, abs=0.01)
    clock.t += 60
    eng.tick()
    assert eng.equity() == pytest.approx(venue.state().account_value, abs=0.05)
    assert eng.available_to_add() < 0.05


def test_paper_top_up_and_promotion_scale_with_bankroll():
    from bot.career import Career
    eng, *_ = make_live([tool_reply("submit_decision", decision("hold"))])
    eng.live = False  # behave like paper for this check
    eng.store.set("account_value", None)
    eng.top_up(eng.clock(), 900)
    assert eng.contributed == 1000 and eng.floor == 500
    c = Career(15.0)
    assert c.summary(eng.store, eng.clock(), eng.bankroll_base)["next_amount"] == 1000


def test_testnet_promotion_needs_spare_test_money():
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("hold"))])
    eng.tick()
    eng.top_up(clock.t, eng.available_to_add() - 0.01)
    eng.store.set("promotion_offer", {"level": 1, "name": "Trader", "amount": 1000.0, "since": 1, "stats": {}})
    with pytest.raises(ValueError, match="test USDC"):
        eng.brain.career.approve(eng, clock.t)


def test_rejected_management_order_doesnt_break_the_tick():
    from tests.test_manage import managed
    eng, venue, clock = make_live([tool_reply("submit_decision", decision("buy", pct=40, stop=95.0)),
                                   tool_reply("submit_decision", decision("hold"))])
    eng.base_g = managed(max_positions=3)
    eng.tick()
    assert "ETH" in eng.positions
    venue.reject = "Order could not immediately match"
    venue.px = 120.0  # past the target: take-profit tries to sell and is rejected
    clock.t += 60
    eng.tick()
    clock.t += 60
    eng.tick()
    warns = [e for e in eng.store.events() if "Trade management for ETH couldn't act" in e["message"]]
    assert len(warns) == 1 and eng.last_error is None


# Maker-first entries

class MakerExchange:
    def __init__(self, alo_status, fills_after_polls=None, partial=0.0):
        self.alo_status, self.orders, self.cancelled = alo_status, [], []
        self.fills_after, self.partial, self.polls = fills_after_polls, partial, 0

    def order(self, coin, is_buy, sz, px, kind, reduce_only=False):
        self.orders.append((coin, is_buy, sz, px, kind))
        if kind["limit"]["tif"] == "Alo":
            self.sz = sz
            return {"status": "ok", "response": {"data": {"statuses": [self.alo_status]}}}
        return {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": str(sz), "avgPx": "100.5"}}]}}}

    def cancel(self, coin, oid):
        self.cancelled.append(oid)


class MakerInfo:
    def __init__(self, ex):
        self.ex = ex

    def meta(self):
        return {"universe": [{"name": "ETH", "szDecimals": 2}]}

    def l2_snapshot(self, coin):
        return {"levels": [[{"px": "99.9", "sz": "5"}], [{"px": "100.1", "sz": "5"}]]}

    def all_mids(self):
        return {"ETH": "100.0"}

    def query_order_by_oid(self, user, oid):
        ex = self.ex
        ex.polls += 1
        if oid in ex.cancelled:
            return {"status": "order", "order": {"status": "canceled", "order": {"origSz": str(ex.sz), "sz": str(ex.sz - ex.partial)}}}
        if ex.fills_after is not None and ex.polls >= ex.fills_after:
            return {"status": "order", "order": {"status": "filled", "order": {"origSz": str(ex.sz), "sz": "0.0"}}}
        return {"status": "order", "order": {"status": "open", "order": {"origSz": str(ex.sz), "sz": str(ex.sz)}}}


def maker_venue(ex):
    from bot.hyperliquid import HyperliquidVenue
    v = HyperliquidVenue("testnet", "0x" + "1" * 40, "0x" + "2" * 64, info=MakerInfo(ex), exchange=ex)
    v._connect = lambda: None
    return v


def test_maker_entry_fills_at_the_bid():
    ex = MakerExchange({"resting": {"oid": 7}}, fills_after_polls=2)
    filled, avg, maker = maker_venue(ex).maker_then_market(True, 1.0, "ETH", wait=20, sleep=lambda s: None)
    assert (filled, avg, maker) == (1.0, 99.9, 1.0)
    assert ex.orders[0][3] == 99.9 and ex.orders[0][4] == {"limit": {"tif": "Alo"}} and len(ex.orders) == 1


def test_maker_entry_part_filled_then_market_for_the_rest():
    ex = MakerExchange({"resting": {"oid": 7}}, partial=0.4)
    filled, avg, maker = maker_venue(ex).maker_then_market(True, 1.0, "ETH", wait=0, sleep=lambda s: None)
    assert ex.cancelled == [7] and maker == pytest.approx(0.4) and filled == pytest.approx(1.0)
    assert ex.orders[1][4] == {"limit": {"tif": "Ioc"}} and ex.orders[1][2] == pytest.approx(0.6)
    assert avg == pytest.approx((0.4 * 99.9 + 0.6 * 100.5) / 1.0)


def test_maker_entry_rejected_goes_straight_to_market():
    ex = MakerExchange({"error": "Post only order would have immediately matched"})
    filled, avg, maker = maker_venue(ex).maker_then_market(False, 1.0, "ETH", wait=20, sleep=lambda s: None)
    assert maker == 0 and filled == 1.0 and ex.orders[1][4] == {"limit": {"tif": "Ioc"}}


def test_live_broker_uses_maker_only_for_entries_and_charges_the_lower_fee():
    from bot.config import CostSettings
    from bot.hyperliquid import LiveBroker
    ex = MakerExchange({"resting": {"oid": 7}}, fills_after_polls=1)
    broker = LiveBroker(maker_venue(ex), CostSettings(), maker_wait=20)
    fill = broker.buy(100.0, 100.0, coin="ETH")
    assert fill.fee == pytest.approx(fill.notional * 0.015 / 100)
    broker.sell(1.0, 100.0, reduce_only=True, coin="ETH")
    assert ex.orders[-1][4] == {"limit": {"tif": "Ioc"}}  # exits stay market orders
