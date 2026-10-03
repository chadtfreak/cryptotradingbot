Backtest of the playbook setups on 20 Hyperliquid coins, 4h candles, Jun 2024 to 03 Oct 2026. R = multiples of the amount risked, after fees. Expectancy is the average R per trade: above about +0.1 is a real edge, below 0 loses money. These are mechanical versions of each setup; your judgement should beat them, but don't ignore them.

setup | trades | win rate | avg win | avg loss | expectancy | last 90 days expectancy (trades)
breakout | 935 | 39% | +1.85R | -1.01R | +0.09R | +0.17R (119)
momentum | 1086 | 39% | +1.77R | -0.98R | +0.08R | +0.27R (108)
failed_breakout | 543 | 35% | +1.91R | -1.04R | -0.00R | -0.21R (64)
pullback | 2332 | 35% | +1.73R | -1.01R | -0.06R | -0.14R (292)
range_fade | 266 | 35% | +1.26R | -1.03R | -0.23R | -0.27R (45)

By direction: setup | long expectancy (trades) | short expectancy (trades)
breakout | +0.15R (456) | +0.03R (479)
momentum | +0.15R (572) | +0.01R (514)
failed_breakout | +0.04R (271) | -0.04R (272)
pullback | -0.08R (1063) | -0.04R (1269)
range_fade | -0.22R (103) | -0.23R (163)

Best and worst coins per setup (at least 8 trades): setup | best | worst
breakout | ENA +0.46R, SAND +0.44R, DOGE +0.43R | LIT -0.25R, XRP -0.25R, WLD -0.22R
momentum | SUI +0.27R, UNI +0.27R, LINK +0.24R | ETH -0.25R, ZRO -0.07R, ONDO -0.04R
failed_breakout | DOGE +0.66R, SAND +0.34R, BTC +0.32R | PUMP -0.33R, ZEC -0.32R, ETH -0.31R
pullback | PUMP +0.12R, BTC +0.07R, WLD +0.06R | XPL -0.32R, UNI -0.16R, ETH -0.16R
range_fade | WLD +0.14R, AAVE +0.08R, XRP +0.01R | UNI -0.54R, SOL -0.46R, ONDO -0.41R

Not backtested: squeeze_fade (needs funding history). Treat it as unproven and size it small.
