# Survival Bot

A crypto trading bot that has to pay its own way. It trades ETH/USDT, pays its hosting costs from its own balance, and dies if it drops below its survival floor. A dashboard shows what it's doing and why.

Currently in **paper mode**: real live prices, pretend money. See [docs/SCOPE.md](docs/SCOPE.md) for the plan and the honest maths.

## Get started

You need Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m bot run
```

Open http://127.0.0.1:8000 and you'll see the dashboard. The bot checks prices every minute and makes its trading decisions every time a 4 hour candle closes, so expect it to be quiet for a while. Every decision shows up in the "What I'm thinking" log.

## Commands

| Command | What it does |
|---|---|
| `python -m bot run` | Start the bot and the dashboard |
| `python -m bot backtest` | Replay the strategy over the last ~120 days of Kraken data |
| `python -m bot backtest --interval 60` | Same, on 1 hour candles |
| `python -m bot reset` | Delete all history and start a new life |
| `pytest` | Run the tests |

## Settings

Everything lives in [config.toml](config.toml): bankroll, strategy, fees and the survival rules. Restart the bot after changing it.

## Running it on a server

The dashboard only listens on your own machine by default. To open it up on a server, set a password first:

```bash
export DASHBOARD_PASSWORD='something long'
python -m bot run --host 0.0.0.0
```

The browser will ask for it (any username works). It will refuse to start on a public address without one.

## How it decides

1. Buy when the 20 EMA crosses above the 50 EMA on a closed 4h candle.
2. Size the trade so hitting the stop loses about 2% of equity.
3. Put the stop 2.5 ATRs below price and drag it up as price rises.
4. Sell when the stop is hit or the 20 EMA drops back below the 50.

On top of that, two survival rules override everything: lose 5% in a day and it sits out until tomorrow; fall below $50 and it sells up and stops for good.

## Layout

```
bot/
  engine.py      the bot: pays bills, checks health, trades
  strategy.py    entry, exit, stop and sizing rules
  backtest.py    replays the strategy over history
  broker.py      paper fills with fees, slippage and gas
  market.py      Kraken price feed
  store.py       SQLite storage
  web.py         dashboard server and API
  static/        dashboard page
tests/           pytest suite
docs/SCOPE.md    plan, decisions and risks
```
