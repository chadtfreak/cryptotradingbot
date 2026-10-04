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

**What it can trade.** Any Hyperliquid perpetual doing over $20M a day (about 20 coins), long or short, up to 10 positions at once. It wakes on 1.5% moves, may take short-term trades off the 1h chart, and decides how often to look (as often as every 15 minutes), seeing how fast it's using its thinking budget. Total exposure stays at 1x its equity, so no leverage sneaks in through several coins. Code scans every liquid coin hourly and wakes Claude when one breaks its 20-day high or low, moves 5% in 4 hours, or has extreme funding (each alert at most once a day per coin).

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
2. Backtested stats (`bot/knowledge/setup_stats.md`) from 30 major coins on Binance's free archive, January 2020 to September 2026 (about 20,000 trades through bull, bear and choppy markets), 4h candles, after fees, split by BTC's trend, the coin's trend, funding and volatility. Breakouts are the one steady edge (+0.10R a trade, still +0.10R in the last 12 months), best in calm markets and with normal funding. Momentum longs make a little (+0.08R), more with BTC rising. Pullbacks (-0.03R), squeeze fades (-0.10R) and range fades (-0.22R) lose money, and the playbook now says so. Refresh with `python -m bot research`.
3. A practice run, started from the dashboard: Claude trades 60 random moments from history with the coin and dates hidden, gets marked on what really happened, and writes lessons it keeps. Costs about $4 to $8 of Anthropic credit, capped at $15, and is billed to your Anthropic account rather than the bot.

## Learning faster (version 1.6)

Real trades come slowly, so a month of trading only teaches a handful of lessons. Four things speed that up:

1. **Shadow calls.** Each time Claude wakes it also makes up to 5 quick calls (direction, stop, target, 4 to 72 hours, how sure it is) on coins it may or may not trade. Code marks them against live prices for free. That's several marked results per wake, for about a cent more.
2. **A scorecard** in front of every decision: its real trades and shadow calls by setup, in R, and how sure it said it was against how often it was right.
3. **A quick review of every closed trade.** The cheaper model writes one lesson right after a trade closes (about a cent), instead of waiting for the weekly review.
4. **Lean calls in the practice run.** On every historical moment, traded or not, it calls which way price moves first, so 60 moments give 60 marked calls instead of a handful of trades.

**Learning phase (October 2026).** For the first 30 days on testnet the owner pays for Claude's thinking, up to $40, instead of it coming out of Claude's $100. It uses the smartest model throughout, and practice runs count towards the $40. When the 30 days or the $40 run out, it goes back to paying its own way under the normal earned allowance. Leverage stays at 1x and the bankroll stays at $100: leverage doesn't speed up learning (results are measured in R), and $100 is the real-money rehearsal.

**What didn't work: a "similar moments" lookup (October 2026).** We tried finding the 50 most similar past moments in 340,000 candles of history and telling Claude what happened next. Tested honestly (only pre 2025 history, predicting 2025 to 2026), it called the direction right 50% of the time, a coin flip, so it isn't used. Short-term crypto direction is close to random; the edge, where there is one, is in picking the right setup for the market and managing risk.

**Bigger bankroll (October 2026).** At $100, Claude's costs (about $16 a month for thinking and hosting) meant it had to make 16% a month just to stay level. The owner can now add to its bankroll from the dashboard, up to whatever is spare in the exchange account. On testnet that's the full $1000. The survival floor moves with it (half the bankroll the owner set), and promotions add 1, 2 and 4 times that bankroll. On testnet a promotion needs that much spare test money in the account. Top-ups and automatic promotion money stay off for real money.

**Funding harvest: not possible yet.** Collecting funding without taking a side needs the spot coin as well as the perp. Hyperliquid's spot markets for BTC, ETH, SOL and HYPE have almost no trading, on mainnet and testnet, and other exchanges need ID. Revisit if those markets come alive.

## Trading like a professional (version 1.12)

- **Trade management.** Every trade gets a take-profit (default twice the amount risked, or Claude's own). Once a trade is up by its risk, the stop moves to breakeven. At the target half is banked and the rest trails one risk-unit behind the best price. A trade that hasn't reached +1R in 5 days is closed. This is what the backtests assumed, so live trading now matches the tested setups. Stops stay on the exchange; targets and trailing are run by the bot every minute.
- **Risk limits.** At most 2% of equity at risk per trade and 10% across all open trades (loss from entry if every stop is hit), replacing full send's 25% per trade. Claude also sees its net long or short, how much its positions behave like one BTC bet, and its open risk.
- **Research matched to now.** The scanner flags any backtested setup firing on each coin, and Claude sees how that setup did over 7 years in the same conditions (BTC trend, the coin's trend, funding, volatility).
- **Trade movement.** Every trade records how far it went for and against before closing, so Claude can tell whether its stops are too tight or it gives back too much.
- **Outside the charts.** Scheduled US economic news (Claude is woken 45 minutes before and just after big releases), open interest change over 24 hours (tracked by the bot from hourly snapshots), and BTC's share of the crypto market. Liquidation and token unlock data need paid feeds, so they aren't included.

## Professional safeguards (version 1.13)

- **Cut risk after losses.** 10% below its best (measured like a fund's unit price, so top-ups don't count), risk per trade halves until a new high. At 20% below, no new trades for 24 hours, once per drawdown.
- **BTC bet cap.** All positions together can't behave like more than 60% of equity in BTC, long or short. Each coin's link to BTC is measured from 20 days of 4h moves.
- **Go-live checklist.** 60+ days, 50+ closed trades, making money after every cost, worst drop under 20%, and beating the maths bot and holding ETH. Shown on the dashboard and to Claude. Unlocking real money is still the owner's decision.
- **Weekly report.** Built by code every Monday (UTC): return against both rivals, worst drop, trades and results by setup, shadow calls, thinking spend, Claude's review and lessons.
- **Cheaper entries.** On Hyperliquid, entries start as a post-only limit order at the best bid or ask (0.015% fee) for up to 20 seconds, then the rest goes at market (0.045%). Exits and stops stay market orders so they always fill.

**The plan from here:** freeze the rules for 4 to 6 weeks (or 50 to 100 closed trades), fix only bugs, then judge with the scorecard, weekly reports and checklist, and change one thing at a time.

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
