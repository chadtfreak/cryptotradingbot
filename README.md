# Survival Bot

Claude trades crypto with its own small account and has to pay its own way. It trades any coin doing over $50M a day on Hyperliquid, long or short, up to 3 at once (all 1x perpetuals), pays for its hosting and every bit of its own thinking from its balance, and dies if it drops below its survival floor. It keeps a journal, reviews itself weekly, earns its thinking budget and promotions by beating its rivals, and comes preloaded with a trading playbook, backtested stats and an optional practice run. A simple maths bot trades alongside it as a rival. A dashboard shows what both are doing and why.

Currently in **paper mode**: real live prices, pretend money. See [docs/SCOPE.md](docs/SCOPE.md) for the plan and the honest maths.

## Run it on your Umbrel

It installs as an Umbrel app with its own home screen icon. See [docs/UMBREL.md](docs/UMBREL.md). This is the best place for it to live long term.

## Run it on your computer

Good for a quick look. The bot only runs while your computer is on and awake.

**1. Install Python** (one time only). Download it from https://www.python.org/downloads/ and install it. On Windows, tick **"Add Python to PATH"** on the first screen of the installer.

**2. Download the bot.** On https://github.com/chadtfreak/cryptotradingbot click the green **Code** button, then **Download ZIP**. Unzip it somewhere easy, like your Desktop.

**3. Open a terminal in that folder.**
- Mac: open the **Terminal** app, type `cd ` (with a space), drag the unzipped folder into the window, press Enter.
- Windows: open the unzipped folder, click the address bar, type `powershell`, press Enter.

**4. Set it up** (one time only). Paste these lines:

Mac:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows:
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

**5. Start the bot:**

Mac: `python -m bot run`

Windows: `.venv\Scripts\python -m bot run`

**6. Open http://127.0.0.1:8000** in your browser. That's the dashboard. Paste your Anthropic API key into the **Claude's brain** panel so Claude can start thinking (see [docs/UMBREL.md](docs/UMBREL.md) for how to get one).

To stop it, go back to the terminal and press **Ctrl+C**. To start it again later, open a terminal in the folder (step 3), then on Mac run `source .venv/bin/activate` followed by `python -m bot run`, or on Windows just run `.venv\Scripts\python -m bot run`.

## Commands

| Command | What it does |
|---|---|
| `python -m bot run` | Start the bot and the dashboard |
| `python -m bot backtest` | Replay the strategy over the last ~120 days of Kraken data |
| `python -m bot backtest --interval 60` | Same, on 1 hour candles |
| `python -m bot research` | Backtest the playbook setups on Hyperliquid history and refresh Claude's stats |
| `python -m bot reset` | Delete all history and start a new life |
| `pip install -r requirements-dev.txt && pytest` | Run the tests |

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
  market.py      Hyperliquid and Kraken price feeds
  claude_brain.py  Claude: when to wake, what it sees, what it decides
  scanner.py     ranks every liquid coin and flags breakouts and big moves
  career.py      allowance, promotions and probation
  research.py    backtests the playbook setups
  practice.py    the historical practice run
  knowledge/     the playbook and backtested stats Claude reads
  store.py       SQLite storage
  web.py         dashboard server and API
  static/        dashboard page
tests/           pytest suite
docs/SCOPE.md    plan, decisions and risks
```
