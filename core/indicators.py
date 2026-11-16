"""Shared technical indicators. Pure functions on pandas objects; no I/O.

Price frames use columns: open, high, low, close, volume (lowercase), indexed by date ascending.
"""

from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1, skipna=False).fillna(df["high"] - df["low"])


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's ATR (RMA of true range)."""
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(s, n)
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "bandwidth": (upper - lower) / mid})


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI. 100 when there are no losses in the window."""
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0)


def avg_dollar_volume(df: pd.DataFrame, n: int = 20) -> pd.Series:
    return (df["close"] * df["volume"]).rolling(n, min_periods=n).mean()


def relative_volume(df: pd.DataFrame, n: int = 50) -> pd.Series:
    """Today's volume / average of the PRIOR n days (excludes today)."""
    return df["volume"] / df["volume"].shift(1).rolling(n, min_periods=n).mean()


def pct_from_high(s: pd.Series, n: int = 252) -> pd.Series:
    """Distance below the rolling n-day high, as a negative fraction (0 = at the high)."""
    return s / s.rolling(n, min_periods=1).max() - 1


def ma_stack(s: pd.Series, periods: tuple[int, ...] = (50, 150, 200)) -> pd.Series:
    """True where SMA(p1) > SMA(p2) > ... strictly, for the given ascending periods."""
    mas = [sma(s, p) for p in periods]
    ok = pd.Series(True, index=s.index)
    for a, b in zip(mas, mas[1:]):
        ok &= a > b
    return ok & mas[-1].notna()


def rs_line(s: pd.Series, benchmark: pd.Series) -> pd.Series:
    """Relative-strength line: price / benchmark, aligned on common dates."""
    a, b = s.align(benchmark, join="inner")
    return a / b


def rs_return(s: pd.Series, benchmark: pd.Series, n: int = 63) -> float:
    """Excess n-day return over the benchmark (stock return minus benchmark return)."""
    a, b = s.align(benchmark, join="inner")
    if len(a) <= n:
        return float("nan")
    return float(a.iloc[-1] / a.iloc[-1 - n] - b.iloc[-1] / b.iloc[-1 - n])


def percentile_rank(values: pd.Series) -> pd.Series:
    """0–100 percentile rank across a cross-section (e.g. RS across the universe)."""
    return values.rank(pct=True) * 100


def is_rising(s: pd.Series, lookback: int = 5) -> pd.Series:
    return s > s.shift(lookback)


def volatility_contraction(df: pd.DataFrame, atr_n: int = 14, lookback: int = 60) -> pd.Series:
    """Current ATR% as a fraction of its max over `lookback` days. Low = tight (squeeze)."""
    atr_pct = atr(df, atr_n) / df["close"]
    return atr_pct / atr_pct.rolling(lookback, min_periods=atr_n).max()


def returns(s: pd.Series) -> pd.Series:
    return s.pct_change()


def max_drawdown(s: pd.Series) -> float:
    peak = s.cummax()
    return float((s / peak - 1).min())
