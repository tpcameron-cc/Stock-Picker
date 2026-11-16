import numpy as np
import pandas as pd
import pytest

from core import indicators as ind
from tests.conftest import make_ohlcv


def test_sma_known_values():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    out = ind.sma(s, 3)
    assert out.isna().sum() == 2
    assert out.iloc[2:].tolist() == [2.0, 3.0, 4.0]


def test_ema_matches_recursive_definition():
    s = pd.Series([10, 11, 12, 13], dtype=float)
    a = 2 / (3 + 1)
    expected = [10.0]
    for x in s.iloc[1:]:
        expected.append(a * x + (1 - a) * expected[-1])
    np.testing.assert_allclose(ind.ema(s, 3).iloc[2:], expected[2:])


def test_true_range_uses_prev_close_gap():
    df = pd.DataFrame({"high": [10, 15], "low": [9, 14], "close": [9.5, 14.5]})
    tr = ind.true_range(df)
    assert tr.iloc[0] == pytest.approx(1.0)      # first bar: high-low
    assert tr.iloc[1] == pytest.approx(5.5)      # gap up: high - prev close


def test_atr_constant_range():
    df = make_ohlcv([100.0] * 40, spread=0.01)   # high-low = 2.0 every day, no gaps
    assert ind.atr(df, 14).iloc[-1] == pytest.approx(2.0)


def test_bollinger_flat_series_has_zero_bandwidth():
    bb = ind.bollinger(pd.Series([50.0] * 25), 20, 2.0)
    assert bb["bandwidth"].iloc[-1] == pytest.approx(0.0)
    assert bb["upper"].iloc[-1] == bb["lower"].iloc[-1] == 50.0


def test_rsi_bounds():
    up = pd.Series(np.arange(1, 40, dtype=float))
    assert ind.rsi(up, 14).iloc[-1] == 100.0
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert ind.rsi(down, 14).iloc[-1] == pytest.approx(0.0)
    zig = pd.Series([10, 11] * 30, dtype=float)
    assert 40 < ind.rsi(zig, 14).iloc[-1] < 60


def test_avg_dollar_volume():
    df = make_ohlcv([10.0] * 25, volume=2_000_000)
    assert ind.avg_dollar_volume(df, 20).iloc[-1] == pytest.approx(20_000_000)


def test_relative_volume_excludes_today():
    vols = [1_000_000] * 50 + [3_000_000]
    df = make_ohlcv([10.0] * 51, volume=vols)
    assert ind.relative_volume(df, 50).iloc[-1] == pytest.approx(3.0)


def test_pct_from_high():
    s = pd.Series([10, 20, 15], dtype=float)
    assert ind.pct_from_high(s, 252).tolist() == pytest.approx([0.0, 0.0, -0.25])


def test_ma_stack_uptrend_and_downtrend():
    up = pd.Series(np.linspace(10, 100, 260))
    assert bool(ind.ma_stack(up).iloc[-1]) is True
    assert bool(ind.ma_stack(up.iloc[::-1].reset_index(drop=True)).iloc[-1]) is False
    assert bool(ind.ma_stack(up).iloc[100]) is False   # not enough history for SMA200


def test_rs_return_and_percentile():
    idx = pd.bdate_range("2025-01-01", periods=70)
    stock = pd.Series(np.linspace(100, 120, 70), index=idx)
    bench = pd.Series(np.linspace(100, 110, 70), index=idx)
    excess = ind.rs_return(stock, bench, 63)
    expected = stock.iloc[-1] / stock.iloc[-64] - bench.iloc[-1] / bench.iloc[-64]
    assert excess == pytest.approx(expected)
    assert excess > 0
    ranks = ind.percentile_rank(pd.Series({"A": 0.1, "B": 0.3, "C": 0.2}))
    assert ranks["B"] == 100 and ranks["A"] < ranks["C"]


def test_rs_return_short_history_is_nan():
    idx = pd.bdate_range("2025-01-01", periods=10)
    s = pd.Series(range(1, 11), index=idx, dtype=float)
    assert np.isnan(ind.rs_return(s, s, 63))


def test_volatility_contraction_detects_squeeze():
    wide = make_ohlcv([100.0] * 60, spread=0.05)
    tight = make_ohlcv([100.0] * 20, spread=0.005, start=wide.index[-1] + pd.offsets.BDay())
    df = pd.concat([wide, tight])
    assert ind.volatility_contraction(df, 14, 60).iloc[-1] < 0.5


def test_max_drawdown():
    assert ind.max_drawdown(pd.Series([100, 120, 90, 130], dtype=float)) == pytest.approx(-0.25)
