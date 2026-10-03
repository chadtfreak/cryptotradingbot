"""Builds Claude's engine for its trading venue, and switches venue from the dashboard.

Each venue is a separate life: switching archives the old database and starts fresh,
but Claude keeps its API key and its lessons learned."""

import os
import time
from pathlib import Path

from .claude_brain import ClaudeBrain
from .engine import Engine
from .market import HyperliquidMarket
from .store import Store

CARRY_OVER = ("anthropic_api_key", "lessons", "lessons_updated", "claude_last_review_ts", "practice_lessons", "practice_report",
              "practice_status", "shadow_results", "trade_records", "trade_lessons",
              "learning_phase", "learning_phase_ended")  # what Claude has learned goes with it
VENUE_NAMES = {"paper": "Paper trading", "testnet": "Hyperliquid testnet", "mainnet": "Hyperliquid (real money)"}


def build_claude(settings, maths: Engine | None, store: Store | None = None) -> Engine:
    store = store or Store(settings.claude.db_path)
    venue = store.get("venue") or {"name": "paper"}
    broker = None
    if venue["name"] in ("testnet", "mainnet"):
        from .hyperliquid import HyperliquidVenue, LiveBroker
        broker = LiveBroker(HyperliquidVenue(venue["name"], venue["account"], venue["agent_key"]), settings.costs)
    brain = ClaudeBrain(settings)
    brain.rival = maths
    return Engine(settings, HyperliquidMarket(settings.bot.asset), store, broker=broker, brain=brain,
                  guardrails=settings.claude_guardrails)


def switch_venue(settings, old: Engine, venue: dict) -> Engine:
    """Archives the current life and starts a new one on the chosen venue."""
    with old.lock:
        if old.qty != 0:
            raise ValueError("Close Claude's position first (use the kill switch), then switch venue.")
        carry = {k: old.store.get(k) for k in CARRY_OVER}
        current = (old.store.get("venue") or {"name": "paper"})["name"]
        path = Path(settings.claude.db_path)
        old.retired = True
        old.store.close()
        if path.exists():
            archive = path.with_name(f"{path.stem}-{current}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}")
            os.replace(path, archive)
        store = Store(settings.claude.db_path)
        for k, v in carry.items():
            if v is not None:
                store.set(k, v)
        store.set("venue", venue)
        engine = build_claude(settings, old.brain.rival, store)
        engine.store.log(f"Switched to {VENUE_NAMES[venue['name']]}. Starting a new life, keeping my lessons learned.", level="warning")
        return engine
