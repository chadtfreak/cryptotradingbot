"""Command line entry point.

    python -m bot run        Start the bot and dashboard
    python -m bot backtest   Replay the strategy over recent Kraken history
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
    args = parser.parse_args()

    settings = load_settings(args.config)

    if args.cmd == "run":
        import uvicorn

        from .engine import Engine
        from .market import KrakenMarket
        from .store import Store
        from .web import create_app

        password = os.environ.get("DASHBOARD_PASSWORD")
        # As an Umbrel app, Umbrel's own login sits in front of the dashboard.
        behind_umbrel = os.environ.get("AUTH_HANDLED_BY_UMBREL") == "true"
        if args.host != "127.0.0.1" and not password and not behind_umbrel:
            raise SystemExit("Refusing to listen on a public address without DASHBOARD_PASSWORD set.")
        if password == "change-me":
            raise SystemExit("Set your own DASHBOARD_PASSWORD (in docker-compose.yml) instead of change-me.")
        engine = Engine(settings, KrakenMarket(settings.bot.pair), Store(settings.bot.db_path))
        print(f"Dashboard: http://{args.host}:{args.port}")
        uvicorn.run(create_app(engine), host=args.host, port=args.port, log_level="warning")

    elif args.cmd == "backtest":
        from .backtest import run_backtest
        from .market import KrakenMarket

        if args.interval:
            settings.bot.interval_minutes = args.interval
        candles = KrakenMarket(settings.bot.pair).candles(settings.bot.interval_minutes)
        print(f"{settings.bot.pair}, {settings.bot.interval_minutes} minute candles, {len(candles)} candles from Kraken\n")
        print(run_backtest(candles, settings).report())

    elif args.cmd == "reset":
        db = Path(settings.bot.db_path)
        if db.exists() and input(f"Delete {db} and all trade history? Type yes: ") == "yes":
            db.unlink()
            print("Done. The next `python -m bot run` starts a new life.")


if __name__ == "__main__":
    main()
