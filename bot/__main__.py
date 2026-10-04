"""Command line entry point.

    python -m bot run        Start the bot and dashboard
    python -m bot backtest   Replay the strategy over recent Kraken history
    python -m bot research   Backtest the playbook setups on years of history (updates Claude's stats)
    python -m bot reset      Wipe the database and start a fresh life
"""

import argparse
import os
from pathlib import Path

from .config import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="bot")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="start the bot and dashboard")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    bt = sub.add_parser("backtest", help="replay the strategy over recent history")
    bt.add_argument("--interval", type=int, help="candle size in minutes (default: from config)")
    sub.add_parser("reset", help="delete all bot data and start again")
    res = sub.add_parser("research", help="backtest the playbook setups on years of history and update Claude's stats")
    res.add_argument("--cache", default="data/history_cache", help="where downloaded history is kept")
    args = parser.parse_args()

    settings = load_settings(args.config)

    if args.cmd == "run":
        import uvicorn

        from .engine import Engine
        from .market import HyperliquidMarket
        from .store import Store
        from .web import create_app

        password = os.environ.get("DASHBOARD_PASSWORD")
        # As an Umbrel app, Umbrel's own login sits in front of the dashboard.
        behind_umbrel = os.environ.get("AUTH_HANDLED_BY_UMBREL") == "true"
        if args.host != "127.0.0.1" and not password and not behind_umbrel:
            raise SystemExit("Refusing to listen on a public address without DASHBOARD_PASSWORD set.")
        if password == "change-me":
            raise SystemExit("Set your own DASHBOARD_PASSWORD (in docker-compose.yml) instead of change-me.")
        maths = Engine(settings, HyperliquidMarket(settings.bot.asset), Store(settings.bot.db_path))
        engines = {"maths": maths}
        if settings.claude.enabled:
            from .venues import build_claude

            engines = {"claude": build_claude(settings, maths), **engines}
        print(f"Dashboard: http://{args.host}:{args.port}")
        uvicorn.run(create_app(engines), host=args.host, port=args.port, log_level="warning")

    elif args.cmd == "backtest":
        from .backtest import run_backtest
        from .market import KrakenMarket

        if args.interval:
            settings.bot.interval_minutes = args.interval
        candles = KrakenMarket(settings.bot.pair).candles(settings.bot.interval_minutes)
        print(f"{settings.bot.pair}, {settings.bot.interval_minutes} minute candles, {len(candles)} candles from Kraken\n")
        print(run_backtest(candles, settings).report())

    elif args.cmd == "research":
        from .history import main as research_main

        research_main(Path(args.cache))

    elif args.cmd == "reset":
        dbs = [p for p in (Path(settings.bot.db_path), Path(settings.claude.db_path)) if p.exists()]
        if dbs and input(f"Delete {', '.join(map(str, dbs))} and all history? Type yes: ") == "yes":
            for db in dbs:
                db.unlink()
            print("Done. The next `python -m bot run` starts a new life.")


if __name__ == "__main__":
    main()
