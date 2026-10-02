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
        return AccountState(value, self.qty, self.entry if self.qty else None)

    def market(self, is_buy, qty, reduce_only):
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

    def cancel_stops(self):
        self.stops = []

    def place_stop(self, qty, stop):
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
    assert eng.store.get("exchange_stop_ok") is True
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
    assert eng.qty == 0 and eng.store.get("stop") is None
    t = eng.store.trades()[0]
    assert t["pnl"] < 0 and "Closed on the exchange" in t["reason"]
    assert 96 < eng.equity(105) < 98  # lost about 5% of the $50 short, plus fees and thinking


def test_close_uses_reduce_only_and_clears_stop():
    eng, venue, clock = make_live([
        tool_reply("submit_decision", decision("long", pct=50, stop=95)),
        tool_reply("submit_decision", {**decision("close"), "close_pct": 100}),
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
    eng.store.set("qty", 0.5)
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
