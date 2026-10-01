"""Technical indicators. Each returns a list the same length as its input,
with None where there isn't enough history yet."""


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current = values[i] * k + current * (1 - k)
        out[i] = current
    return out


def atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    """Average True Range, Wilder smoothing."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    current = sum(trs[1 : period + 1]) / period
    out[period] = current
    for i in range(period + 1, n):
        current = (current * (period - 1) + trs[i]) / period
        out[i] = current
    return out
