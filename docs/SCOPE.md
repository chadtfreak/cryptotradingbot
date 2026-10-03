# Survival Bot: scope

## The idea

A crypto trading bot with its own USDT wallet that has to earn its keep. It pays its own running costs out of its balance. If its balance falls below a floor, it dies: it sells everything and stops for good. You watch it live on a dashboard and can pull the plug at any time.

## Decisions so far

| Question | Decision |
|---|---|
| Where it trades | Hyperliquid's ETH perpetual at 1x (no leverage), from a self-custody wallet with a trade-only API key. No KYC. Paper trading models its fees and live funding. |
| Who you are | Australian. Every trade is a CGT event, so the bot records the USDT/AUD rate at every trade and exports a CSV for tax time. |
| Bankroll | $100 USDC |
| Control | Fully autonomous, with a kill switch on the dashboard |
| Survival rule | Running costs are paid from the bot's balance before anything counts as profit. Below $50 it dies. |
| Hosting | Your Umbrel at home. The bot charges itself $1 a month as its share of the power bill. |
| The trader | Claude, via the Anthropic API. It pays for its own thinking out of its balance, capped at $15 a month. |
| The rival | The original maths bot keeps paper trading alongside Claude, with its own $100, as a benchmark. |

## The honest maths

$100 is a small bankroll for a bot that pays rent. At $6 a month for a VPS, the bot needs to make **6% a month (about 72% a year) just to stand still**. Very few strategies do that reliably.

The first backtest on the last 111 days of ETH/USDT (4h candles) showed this clearly:

- the strategy itself made about **+18%** from trading
- hosting costs took about **22%** of the starting balance
- net result **-3.5%**, while simply holding ETH made +61% over the same period

So the strategy wasn't the main problem at this size. The rent was.

**Decision:** run it on the Umbrel and drop running costs to $1 a month. The same backtest then finishes at **+16.7%** (costs $3.67 instead of $22), and if it made nothing at all it would take about 4 years to hit the floor.

## Claude as the trader

The idea: not just a formula, but a Claude agent that trades, learns and fights to stay alive.

**What it can and can't do.** Nobody can promise top 0.01% results, and Claude has no proven edge in markets. What it brings is judgement across the whole picture (both timeframes, BTC, sentiment, news), discipline, and an honest record of why it did what it did. The maths bot running alongside it, plus buy and hold, tells us whether that's worth paying for.

**How it decides when to think.** Code watches the market every minute for free. Claude is only woken when something is worth a decision:

- a 20/50 EMA crossover on the 4h chart
- a 3% move since its last check
- price within one ATR of its stop
- its own scheduled check-in (it chooses 2 to 48 hours ahead, default daily)

**How it pays for itself.** Every call is priced from the API's usage numbers and charged to the bot's own balance. A hard cap of $15 a month sits on top. It changes how it thinks as money gets tight:

| Mode | When | Model | News search |
|---|---|---|---|
| Sharp | Health 60%+ and spending on pace | Opus 5.5 | up to 2 per check |
| Lean | Otherwise | Sonnet 5.5 | none |
| Survival | Health under 25% or under $2 of budget left | Sonnet 5.5, only for real events | none |
| Asleep | Under $0.50 of budget left | none until next month | coded stops still work |

Health is how far equity sits between the $50 floor (0%) and the $100 start (100%). A check costs roughly 3 to 13 cents, so a normal month should land around $5 to $8.

**How it learns.** Every decision is stored with its reasoning and a note to its future self. Once a week it reviews its decisions, its trades, and how it went against the maths bot and buy and hold, then rewrites a short "lessons learned" note that it reads before every decision.

**What it can't override.** Each bot has its own rulebook in `config.toml`, enforced in code. You chose "full send" for Claude, so its rules are deliberately loose. The maths bot stays careful, which makes the race a bold trader against a disciplined formula.

| Rule | Claude (full send) | Maths bot (careful) |
|---|---|---|
| Stop required on every buy | yes, 0.5% to 50% below price | yes, 1% to 15% below price |
| Max loss if the stop is hit | 25% of equity | 3% of equity (uses 2%) |
| Trades per day | no limit | 4 |
| Daily loss limit | none | 5%, then sits out until tomorrow |
| Add to a position | yes | no |
| Partial sells | yes | no |
| Move the stop | either way | tighten only |
| Go short | yes | no |
| Leverage | none, 1x cap in code | none |
| $50 survival floor | yes | yes |

Claude's trading personality follows its rulebook: with "full send" it is told to hunt for trades, size up with conviction, add to winners and take partial profits, while sizing down as its health drops. It also wakes on 2% moves (not 3%), can check in as often as hourly, and sees the 1 hour chart. Its $15 monthly thinking cap is unchanged, so busier months push it onto the cheaper model sooner.

## Trading anything liquid, earning its keep, and pre-learning (version 1.5)

**What it can trade.** Any Hyperliquid perpetual doing over $50M a day, long or short, up to 3 positions at once. Total exposure stays at 1x its equity, so no leverage sneaks in through several coins. Code scans every liquid coin hourly and wakes Claude when one breaks its 20-day high or low, moves 5% in 4 hours, or has extreme funding (each alert at most once a day per coin).

**How it earns its brain.** At the start of each month it's graded against its two rivals, the maths bot and simply holding ETH:

| Last month | This month's thinking allowance | Smartest model |
|---|---|---|
| Beat both | full $15 | yes |
| Beat one | $10 | yes |
| Beat neither | $6 | no, cheaper model only |

Two losing months in a row puts it on probation: half the risk per trade and one position at a time, until it has a winning month that beats at least one rival.

**Promotion ladder.** Rookie, Trader, Senior trader, Partner. A promotion needs 30 days of at least +10%, beating both rivals, with 5 or more closed trades. It shows up in the dashboard and only happens if you approve it, which adds $100, $200 or $400 to its paper or testnet bankroll. On real money you'd top up the account yourself.

**Pre-learning.** Three layers, all read before every decision:

1. A written playbook (`bot/knowledge/playbook.md`): regimes, BTC leading, funding and liquidations, six setups with entries and stops, sizing, costs and common mistakes.
2. Backtested stats (`bot/knowledge/setup_stats.md`) from 20 coins, mid 2024 to October 2026, 4h candles, after fees. Breakouts (+0.09R a trade) and momentum (+0.08R) had a small real edge, mostly on the long side, and were stronger in the last 90 days. Pullbacks (-0.06R) and range fades (-0.23R) lost money. Refresh with `python -m bot research`.
3. A practice run, started from the dashboard: Claude trades 60 random moments from history with the coin and dates hidden, gets marked on what really happened, and writes lessons it keeps. Costs about $4 to $8 of Anthropic credit, capped at $15, and is billed to your Anthropic account rather than the bot.

## Stages

### Stage 1: paper trading (built, Claude added in version 1.1)

- Live ETH/USDT prices from Kraken's public API
- Trend following strategy: EMA 20/50 crossover entries, trailing ATR stop, 2% risk per trade, spot only, no leverage
- Realistic costs: 0.05% pool fee, 0.10% slippage and $0.05 gas per swap
- Survival ledger: running costs charged hourly, life-left estimate, death below the floor
- Daily loss limit: down 5% in a UTC day means it sells up and sits out until tomorrow
- Dashboard: equity, P&L split into trading and costs, life left, position, chart, trades, a plain-English log of every decision, kill switch, CSV export
- Backtester using exactly the same strategy and cost code

**Exit criteria:** at least 4 weeks of paper trading where trading profit covers running costs, with no bugs in the log.

### Why Hyperliquid (decided October 2026)

Researched and chosen over Uniswap because:

- stop orders live on the exchange, so they fire even if the Umbrel is offline
- shorting, through the ETH perpetual, kept at 1x with no leverage
- trade-only API keys that can't withdraw, so a leaked key can't drain the account
- low fees (0.045% taker on perps, no gas) and a free testnet
- open to Australians with no KYC (only the US, Ontario and sanctioned countries are restricted)

Risks accepted: a young platform with a small validator set and past interventions (the JELLY and POPCAT incidents in 2025, both on small memecoins), and no Australian consumer protection. ASIC treats perps much like CFDs, and tax treatment may differ from spot, so check with an accountant before going live. Only keep the bot's own money there.

### Stage 2: live on Hyperliquid

1. Paper trading with Hyperliquid fees, funding and shorting (done in version 1.3).
2. Run against Hyperliquid's testnet with pretend funds to prove orders, on-exchange stops and the API key work (built in version 1.4, see [HYPERLIQUID.md](HYPERLIQUID.md)).
3. Create a trade-only API wallet, kept on the Umbrel. The main wallet key never touches it.
4. Deposit $100 USDC from Arbitrum and switch Claude to live. The maths bot can stay on paper as the benchmark.

### Stage 3: nice to haves

- Phone alerts on trades, the daily loss limit and death
- Simple deploy script for a VPS or Raspberry Pi
- More strategies, chosen by backtest and paper results rather than gut feel
- AUD figures throughout the dashboard

## Risks

- **Smart contract and wallet risk.** A hot wallet on a server can be drained if the server is compromised. Keep only what the bot needs in it.
- **Strategy risk.** Trend following loses in choppy, sideways markets, often several trades in a row.
- **Small sample.** One 111 day backtest with 7 trades proves very little either way.
- **Tax.** The CSV helps, but check with an accountant how the ATO treats your situation.
