"""Trending coins: reading CoinGecko's list and following quiet coins to see if attention leads price."""

import pytest

from bot import attention
from bot.store import Store
from tests.test_claude import tool_reply
from tests.test_multi import actions, make_multi

H = 3600


class FakeMacro:
    def __init__(self, rows):
        self.rows = rows

    def trending(self):
        return self.rows

    def events(self, impact=("High", "Medium")):
        return []

    def btc_dominance(self):
        return None


def row(symbol, rank, change=1.0):
    return {"symbol": symbol, "name": symbol.title(), "rank": rank, "market_cap_rank": 50, "change_24h": change}


UNIVERSE = [{"coin": "SOL", "mark": 20.0, "prev_day": 19.8, "volume": 300e6},
            {"coin": "kPEPE", "mark": 0.01, "prev_day": 0.008, "volume": 90e6},
            {"coin": "MEME", "mark": 1.0, "prev_day": 1.0, "volume": 5e6}]


def test_new_quiet_trending_coin_is_followed_and_scored():
    st = Store(":memory:")
    attention.refresh(st, FakeMacro([row("BTC", 1)]), UNIVERSE, {"SOL", "kPEPE"}, -2 * H)  # baseline: nothing is new yet
    assert st.get("attention_watch") == []
    macro = FakeMacro([row("SOL", 1), row("PEPE", 2, 25.0), row("WISP", 3)])
    rows = attention.refresh(st, macro, UNIVERSE, {"SOL", "kPEPE"}, 0)
    by = {r["symbol"]: r for r in rows}
    assert by["SOL"]["coin"] == "SOL" and by["PEPE"]["coin"] == "kPEPE" and by["WISP"]["coin"] is None
    watch = st.get("attention_watch")
    assert [w["coin"] for w in watch] == ["SOL"]  # kPEPE already moved 25%, WISP isn't on Hyperliquid
    assert attention.refresh(st, macro, UNIVERSE, set(), 10 * 60) is None  # hourly
    attention.settle(st, {"SOL": 22.0}, 25 * H)
    assert st.get("attention_watch")[0]["ret_24h"] == pytest.approx(10.0)
    done = attention.settle(st, {"SOL": 21.0}, 73 * H)
    assert done[0]["ret_72h"] == pytest.approx(5.0) and done[0]["max_up"] == pytest.approx(10.0)
    s = attention.stats(st)
    assert s["n"] == 1 and s["up_72h"] == 100 and s["open"] == 0


def test_coin_already_on_the_list_isnt_new_again():
    st = Store(":memory:")
    macro = FakeMacro([row("SOL", 1)])
    st.set("trending_last", [])
    attention.refresh(st, macro, UNIVERSE, {"SOL"}, 0)
    assert len(st.get("attention_watch")) == 1
    st.set("attention_watch", [])
    rows = attention.refresh(st, macro, UNIVERSE, {"SOL"}, 2 * H)
    assert not rows[0]["new"] and st.get("attention_watch") == []


def test_claude_sees_trending_coins_and_dashboard_gets_them():
    eng, fake, market, clock = make_multi([tool_reply("submit_decisions", actions())])
    eng.brain.c.trending_feed = True
    eng.brain.macro = FakeMacro([row("SOL", 1), row("WISP", 2)])
    eng.store.set("trending_last", ["BTC"])
    eng.tick()
    ctx = fake.requests[0]["messages"][0]["content"]
    assert "## Trending on CoinGecko" in ctx and "#1 SOL (SOL on Hyperliquid)" in ctx and "NEW" in ctx
    assert "Also trending but not on Hyperliquid: WISP" in ctx
    brain = eng.summary()["brain"]
    assert brain["trending"][0]["symbol"] == "SOL" and brain["attention"]["open"] == 1
