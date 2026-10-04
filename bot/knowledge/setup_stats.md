Backtest of the playbook setups on 30 major coins, Binance 4h candles, Jan 2020 to Sep 2026, through bull, bear and choppy markets. R = multiples of the amount risked, after fees, 1.5 ATR stop and 2 to 3 ATR target. Expectancy is the average R per trade: above about +0.1 is a real edge, below 0 loses money. Numbers in brackets are trade counts. These are mechanical versions of each setup and base rates, not promises; where a setup only works in one kind of market, trade it only there. Splits with fewer than about 300 trades are noisy, so don't lean hard on a single small cell. Where these numbers disagree with the playbook, trust the numbers.

setup | trades | win rate | expectancy | long | short | last 12 months
breakout | 3503 | 39% | +0.10R | +0.09R (1875) | +0.11R (1628) | +0.10R (679)
momentum | 3795 | 37% | +0.04R | +0.08R (2062) | -0.02R (1733) | -0.04R (563)
failed_breakout | 1877 | 36% | -0.00R | +0.08R (791) | -0.06R (1086) | -0.15R (370)
pullback | 9263 | 36% | -0.03R | -0.08R (4394) | +0.01R (4869) | -0.05R (1798)
squeeze_fade | 1305 | 33% | -0.10R | -0.13R (746) | -0.06R (559) | -0.13R (254)
range_fade | 1090 | 35% | -0.22R | -0.20R (422) | -0.23R (668) | -0.11R (241)

When BTC's 4h trend is up or down: setup and side | BTC up | BTC down
breakout long | +0.10R (1665) | +0.01R (210)
breakout short | +0.23R (281) | +0.08R (1347)
momentum long | +0.11R (1615) | -0.02R (447)
momentum short | +0.03R (443) | -0.03R (1290)
failed_breakout long | -0.12R (90) | +0.11R (701)
failed_breakout short | -0.09R (972) | +0.16R (114)
pullback long | -0.04R (3568) | -0.24R (826)
pullback short | +0.05R (1556) | -0.01R (3313)
squeeze_fade long | -0.17R (150) | -0.12R (596)
squeeze_fade short | -0.06R (531) | +0.07R (28)
range_fade long | -0.23R (248) | -0.15R (174)
range_fade short | -0.29R (461) | -0.10R (207)

When the coin itself is trending (20 EMA at least 1 ATR from the 50) or choppy: setup and side | trending | choppy
breakout long | +0.06R (979) | +0.12R (896)
breakout short | -0.03R (669) | +0.20R (959)
momentum long | +0.05R (602) | +0.09R (1460)
momentum short | -0.15R (447) | +0.03R (1286)
failed_breakout long | +0.15R (513) | -0.04R (278)
failed_breakout short | -0.05R (788) | -0.09R (298)
pullback long | -0.12R (1419) | -0.06R (2975)
pullback short | -0.04R (1477) | +0.03R (3392)
squeeze_fade long | -0.04R (333) | -0.20R (413)
squeeze_fade short | -0.01R (323) | -0.12R (236)
range_fade long | none | -0.20R (422)
range_fade short | none | -0.23R (668)

By funding (longs crowded = over 30% a year, shorts crowded = negative): setup and side | longs crowded | normal | shorts crowded
breakout long | -0.04R (362) | +0.12R (1313) | +0.12R (200)
breakout short | +0.25R (50) | +0.19R (963) | -0.04R (615)
momentum long | +0.05R (425) | +0.09R (1396) | +0.09R (241)
momentum short | +0.41R (56) | -0.05R (993) | -0.00R (684)
failed_breakout long | -0.39R (14, too few) | +0.02R (382) | +0.17R (395)
failed_breakout short | -0.09R (256) | -0.04R (735) | -0.20R (95)
pullback long | +0.02R (572) | -0.09R (3117) | -0.09R (705)
pullback short | +0.01R (165) | +0.05R (3352) | -0.10R (1352)
squeeze_fade long | none | none | -0.13R (746)
squeeze_fade short | -0.06R (559) | none | none
range_fade long | -0.41R (30) | -0.26R (277) | -0.00R (115)
range_fade short | +0.03R (26) | -0.26R (537) | -0.19R (105)

When the coin is more or less volatile than usual: setup and side | high vol | low vol
breakout long | +0.02R (965) | +0.16R (910)
breakout short | +0.09R (920) | +0.12R (708)
momentum long | +0.04R (1398) | +0.17R (664)
momentum short | -0.03R (1519) | +0.09R (214)
failed_breakout long | +0.20R (482) | -0.09R (309)
failed_breakout short | -0.04R (676) | -0.10R (410)
pullback long | -0.08R (2262) | -0.07R (2132)
pullback short | -0.04R (2235) | +0.05R (2634)
squeeze_fade long | -0.09R (380) | -0.16R (366)
squeeze_fade short | -0.03R (450) | -0.16R (109)
range_fade long | -0.09R (230) | -0.32R (192)
range_fade short | -0.07R (153) | -0.28R (515)

squeeze_fade here means: funding over 40% a year with RSI over 65 near the 20-day high (short), or funding below -15% with RSI under 35 near the 20-day low (long). Binance funding is used as the measure of crowding.
