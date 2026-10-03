# Survival Bot trading playbook

This is your manual. It's a starting point, not scripture: where your own lessons and the backtested setup stats disagree with it, trust the evidence.

## 1. What makes money at this size

- You have about $100 and no leverage. A good trade makes a few dollars. Your edge has to beat fees (about 0.1% per round trip on Hyperliquid perps), slippage, funding and your own thinking costs.
- So: fewer, better trades with room to move. A trade aiming for less than about 3x its costs isn't worth taking.
- The goal is positive expectancy: (win rate x average win) minus (loss rate x average loss), after costs. A 40% win rate is excellent if winners are 2.5x the size of losers. A 70% win rate loses money if losers are 3x the winners.
- Always define three numbers before entering: entry, stop (where you are wrong), and target (where the idea has played out). Reward to risk of at least 2:1 unless the setup stats say a particular setup works at less.

## 2. Reading the market

**Regime first.** Before any setup, decide which world you're in:
- Trending: 4h 20 EMA clearly above or below the 50 EMA, price making higher highs (or lower lows), daily chart agreeing. Trade with the trend: breakouts and pullbacks.
- Ranging: EMAs flat and tangled, price bouncing between levels. Fade the edges or stand aside. Breakouts fail more often here.
- Volatile chop: big candles both ways, no follow-through. The best trade is usually no trade.

**BTC leads.** Most coins follow BTC. Don't go long an altcoin while BTC is breaking down, and be careful shorting one while BTC is ripping. An altcoin rising while BTC falls is showing real relative strength, and vice versa.

**Timeframes.** The daily chart sets the direction, the 4h finds the setup, the 1h times the entry. When they disagree, size down or wait.

## 3. Crypto and perp specifics

- **Funding** is the cost of holding a perp. Positive funding means longs pay shorts. Very high positive funding (above about +50% a year) means longs are crowded and a long squeeze is likely if price stalls. Very negative funding means shorts are crowded and a short squeeze is likely. Crowded trades unwind violently.
- **Open interest** rising with price means new money is pushing the move (healthier). Price rising while open interest falls is shorts covering (often fades). A sharp open interest drop after a big candle means a liquidation cascade, which often marks a short-term extreme.
- **Liquidation cascades** overshoot. After a fast flush with a long wick, the next few hours often retrace part of it. Don't chase the flush.
- **Weekends and the Asian session** are thinner. Breakouts on low volume fail more often. US market open (around 13:30 to 14:30 UTC) and major US data releases (inflation, jobs, Fed decisions) bring sharp moves.
- **New listings, memecoins and low-volume coins** are easily manipulated. You can only trade liquid coins (see your rules for the volume limit), but even within that list, the smaller ones move harder and gap through stops.
- **Round numbers and prior highs and lows** attract stops and orders. Price often wicks just through them before reversing.

## 4. Setups

Tag every trade with the setup it uses, so the stats can learn what works.

**Breakout (trend continuation).** Price closes a 4h candle above the 20-day high (or below the 20-day low for a short), ideally with the trend already pointing that way and volume above normal. Enter on the close or a small pullback. Stop below the breakout level or about 1.5 ATR away. Target 2 to 3 ATR, then trail. Fails often in ranges: check the regime.

**Pullback in a trend.** In a clear uptrend, price pulls back to the 4h 20 EMA or a prior breakout level and holds (a higher low forms). Enter on the bounce, stop just below the pullback low, target the prior high or beyond. The mirror image works for shorts in downtrends. Usually the best reward to risk.

**Range fade.** In a clearly ranging market, short near the top of the range and buy near the bottom, with the stop just outside the range and the target at the middle or other side. Only when the regime really is a range.

**Squeeze fade (funding).** When funding is extreme and price stops making progress in the crowded direction, position against the crowd with a tight stop beyond the recent extreme. High risk, high reward, small size.

**Failed breakout.** Price breaks a key level, then closes back inside within a candle or two. Trapped traders add fuel the other way. Enter on the close back inside, stop beyond the failed break's extreme.

**Momentum continuation.** A coin up (or down) strongly on the day and holding near its extreme into a fresh 4h close, with BTC supportive. Smaller size, tighter trailing stop. Late entries in exhausted moves (RSI above about 80 on the 4h) are traps.

## 5. Risk and position sizing

- Size from the stop, not from conviction alone: decide how much of equity you're willing to lose if the stop is hit (1% to 5% is typical; more only with high conviction and a proven setup), then position size = risk amount / stop distance.
- Correlated positions are one bet. Long BTC, ETH and SOL together is roughly one big long crypto position. Spread risk across genuinely different ideas, or size each smaller.
- Cut losers at the stop, every time. Moving a stop further away to avoid being wrong is the most common way traders die.
- Let winners run: after a trade moves 1R in your favour, move the stop to breakeven or better. Take partial profits at 2R and trail the rest.
- Add to winners, never to losers.
- After two or three losses in a row, size down until a win. Losing streaks happen to every strategy; the goal is to survive them.
- As your health falls toward the floor, size down. Near the floor, preservation comes first.

## 6. Thinking costs and when to wake

- Every check costs real money. If nothing has changed, a quick hold with a longer next_check_hours is a good decision.
- Ask to be woken sooner only when something specific could happen (a level about to break, a funding reset, a data release).
- Use news search when something unexplained is happening (a coin moving 10% on no chart reason) or before a known event, not routinely.

## 7. Common mistakes to avoid

- Overtrading: taking marginal setups because you were woken up. Most wake-ups should end in "hold".
- Revenge trading after a loss.
- Chasing a move that's already extended (far from the EMAs, RSI extreme).
- Fighting the trend because it "has to" reverse.
- Ignoring BTC.
- Holding a losing trade because the reason has changed into a new reason.
- Taking profits too early on winners while letting losers run to the stop.
- Confusing a lucky outcome with a good decision, or a good decision with a bad outcome. Judge decisions by the process.
