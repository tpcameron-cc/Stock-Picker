import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from scanners.breakout.backtest import simulate_trade, summarize
from scanners.breakout.setups import (
    Plan, compute_features, detect_cup_handle, make_plan, passes_gates, technical_score,
)


@pytest.fixture
def b():
    return load_settings(DEFAULT_SETTINGS).section("breakout")


def frame(closes, volume=1_000_000, spread=0.01):
    closes = np.asarray(closes, dtype=float)
    vol = np.full(len(closes), volume, dtype=float) if np.isscalar(volume) else np.asarray(volume, float)
    return pd.DataFrame({"open": closes, "high": closes * (1 + spread), "low": closes * (1 - spread),
                         "close": closes, "volume": vol},
                        index=pd.bdate_range("2023-01-02", periods=len(closes)))


def uptrend_then_base(base_days=30, breakout=False, rvol=3.0):
    """Steady 300-day uptrend, then a tight flat base, optionally a volume breakout on the last bar."""
    trend = np.linspace(50, 100, 300)
    base = 100 + 1.0 * np.sin(np.linspace(0, 6, base_days))         # +-1% chop
    closes = np.concatenate([trend, base])
    vol = np.full(len(closes), 1_000_000.0)
    if breakout:
        closes = np.append(closes, 104.0)
        vol = np.append(vol, 1_000_000.0 * rvol)
    return frame(closes, vol, spread=0.005)


def bench(n):
    return pd.Series(np.linspace(100, 110, n), index=pd.bdate_range("2023-01-02", periods=n))


def test_features_are_causal(b):
    """Feature values on day t must not change when future bars are appended (no look-ahead)."""
    df = uptrend_then_base(40, breakout=True)
    full = compute_features(df, bench(len(df)), b)
    cut = len(df) - 15
    partial = compute_features(df.iloc[:cut], bench(len(df)).iloc[:cut], b)
    cols = ["pivot", "base_low", "sma50", "atr", "rs_ret", "flat_base", "vcp", "squeeze", "breakout_day",
            "pullback", "cup_handle", "ma_stack", "rs_new_high"]
    pd.testing.assert_frame_equal(full[cols].iloc[:cut], partial[cols], check_dtype=False)


def test_pivot_excludes_today_and_breakout_detected(b):
    df = uptrend_then_base(30, breakout=True)
    f = compute_features(df, bench(len(df)), b).iloc[-1]
    assert f["pivot"] == pytest.approx(df["high"].iloc[-26:-1].max())
    assert f["ma_stack"] and f["flat_base"] and f["breakout_day"]
    assert f["rvol"] == pytest.approx(3.0)


def test_breakout_needs_volume(b):
    df = uptrend_then_base(30, breakout=True, rvol=1.2)
    assert not compute_features(df, bench(len(df)), b).iloc[-1]["breakout_day"]


def test_downtrend_fails_gates(b):
    df = frame(np.linspace(100, 50, 320))
    row = compute_features(df, bench(len(df)), b).iloc[-1]
    fails = passes_gates(row, 90.0, b)
    assert any("MA stack" in x for x in fails) and any("52w high" in x for x in fails)
    assert any("RS percentile" in x for x in passes_gates(row, 50.0, b))


def test_breakout_plan_is_coherent(b):
    df = uptrend_then_base(30, breakout=True)
    row = compute_features(df, bench(len(df)), b).iloc[-1]
    plan, why = make_plan(row, b)
    assert why is None and plan.setup == "breakout"
    assert plan.entry_low == pytest.approx(row["pivot"])
    assert plan.entry_high == pytest.approx(row["pivot"] * 1.05)
    assert plan.stop < plan.entry_low < plan.entry_high < plan.target
    assert plan.stop == pytest.approx(max(row["base_low"], row["pivot"] - 2 * row["atr"]))


def test_extended_breakout_rejected(b):
    df = uptrend_then_base(30)
    df = pd.concat([df, frame([115.0], 5_000_000, 0.005).set_axis([df.index[-1] + pd.offsets.BDay()])])
    row = compute_features(df, bench(len(df)), b).iloc[-1]
    plan, why = make_plan(row, b)
    assert plan is None and "extended" in why


def test_pre_breakout_plan_and_distance(b):
    df = uptrend_then_base(30)
    row = compute_features(df, bench(len(df)), b).iloc[-1]
    plan, why = make_plan(row, b)
    assert plan is not None and plan.setup == "pre_breakout"
    assert plan.distance_to_pivot == pytest.approx(row["pivot"] / row["close"] - 1)
    assert plan.cancel_below == pytest.approx(row["base_low"])


def test_score_capped_and_monotone(b):
    df = uptrend_then_base(30, breakout=True)
    row = compute_features(df, bench(len(df)), b).iloc[-1]
    assert technical_score(row, 99, b) > technical_score(row, 85, b)
    assert technical_score(row, 79, b) < technical_score(row, 85, b)


def test_cup_handle_detector(b):
    p = b["base"]
    left = np.full(10, 100.0)
    cup = np.concatenate([np.linspace(100, 75, 30), np.linspace(75, 99, 30)])   # 25% deep
    handle = np.array([99, 97, 95, 94, 95, 96, 97, 98], dtype=float)
    pre = np.linspace(60, 100, 120)
    close = np.concatenate([pre, left, cup, handle])
    vol = np.concatenate([np.full(len(pre) + len(left) + len(cup), 2e6), np.full(len(handle), 1e6)])
    assert detect_cup_handle(close, vol, p)
    loud = vol.copy()
    loud[-len(handle):] = 5e6
    assert not detect_cup_handle(close, loud, p)                     # handle on heavy volume
    deep = close.copy()
    deep[-len(handle):] = 80.0
    assert not detect_cup_handle(deep, vol, p)                       # handle too deep / low in cup


# ---------------- backtest simulation ----------------

def arrays(rows):
    o, h, l, c = (np.array(x, dtype=float) for x in zip(*rows))
    return o, h, l, c


def plan(setup="breakout", lo=100, hi=105, stop=95, target=115, cancel=None):
    return Plan(setup, lo, hi, stop, target, "test", lo, 0.0, [], cancel_below=cancel)


def test_target_hit():
    o, h, l, c = arrays([(99, 100, 98, 100), (101, 103, 100, 102), (103, 116, 102, 114)])
    fi, xi, fpx, xpx, why = simulate_trade(o, h, l, c, 0, plan(), 5, 30)
    assert (fi, xi, why) == (1, 2, "target") and fpx == 101 and xpx == 115


def test_gap_through_stop_fills_at_open():
    o, h, l, c = arrays([(99, 100, 98, 100), (101, 103, 100, 102), (90, 92, 89, 91)])
    *_, xpx, why = simulate_trade(o, h, l, c, 0, plan(), 5, 30)
    assert why == "stop" and xpx == 90


def test_same_bar_stop_first():
    o, h, l, c = arrays([(99, 100, 98, 100), (101, 103, 100, 102), (102, 120, 90, 110)])
    assert simulate_trade(o, h, l, c, 0, plan(), 5, 30)[4] == "stop"
    assert simulate_trade(o, h, l, c, 0, plan(), 5, 30, same_bar_rule="target_first")[4] == "target"


def test_time_stop_and_open_at_end():
    rows = [(99, 100, 98, 100)] + [(101, 102, 100, 101)] * 5
    o, h, l, c = arrays(rows)
    assert simulate_trade(o, h, l, c, 0, plan(), 5, 3)[4] == "time"
    assert simulate_trade(o, h, l, c, 0, plan(), 5, 30)[4] == "open_at_end"


def test_gap_above_zone_skips_breakout():
    o, h, l, c = arrays([(99, 100, 98, 100), (110, 112, 109, 111)])
    assert simulate_trade(o, h, l, c, 0, plan(), 5, 30) is None


def test_pre_breakout_buy_stop():
    p = plan("pre_breakout", stop=97, cancel=90)
    # dips below the stop while pending (allowed), then triggers intrabar and closes strong
    o, h, l, c = arrays([(96, 97, 95, 96), (96, 97, 94, 95), (96, 101, 95.5, 100.5), (101, 102, 100, 101)])
    fi, xi, fpx, xpx, why = simulate_trade(o, h, l, c, 0, p, 5, 2)
    assert fi == 2 and fpx == 100 and why == "time"            # fill-bar low 95.5 < stop ignored: close held
    # undercut of the base low before trigger cancels
    o, h, l, c = arrays([(96, 97, 95, 96), (92, 93, 89, 90), (96, 101, 95, 100)])
    assert simulate_trade(o, h, l, c, 0, p, 5, 30) is None
    # never triggers within pending window
    o, h, l, c = arrays([(96, 97, 95, 96)] * 4)
    assert simulate_trade(o, h, l, c, 0, p, 2, 30) is None


def test_slippage_applied():
    o, h, l, c = arrays([(99, 100, 98, 100), (100, 103, 100, 102), (103, 116, 102, 114)])
    _, _, fpx, xpx, _ = simulate_trade(o, h, l, c, 0, plan(), 5, 30, slippage=0.01)
    assert fpx == pytest.approx(101.0) and xpx == pytest.approx(115 * 0.99)


def test_summarize_math():
    t = pd.DataFrame({
        "setup": ["breakout", "breakout", "pullback", "pullback"],
        "ret": [0.10, -0.05, 0.04, -0.02], "r_multiple": [2.0, -1.0, 1.0, -0.5],
        "spy_ret": [0.01, 0.0, 0.02, -0.01], "exit_reason": ["target", "stop", "time", "stop"],
        "bars_held": [10, 3, 30, 5],
    })
    s = summarize(t)
    assert s.loc["ALL", "trades"] == 4
    assert s.loc["ALL", "hit_rate"] == 0.5
    assert s.loc["ALL", "expectancy_pct"] == pytest.approx(0.0175)
    assert s.loc["ALL", "expectancy_r"] == pytest.approx(0.375)
    assert s.loc["breakout", "avg_gain"] == pytest.approx(0.10)
    assert s.loc["breakout", "avg_loss"] == pytest.approx(-0.05)
    assert s.loc["ALL", "avg_spy_same_period"] == pytest.approx(0.005)       # (0.01+0+0.02-0.01)/4
    assert s.loc["ALL", "avg_excess_vs_spy"] == pytest.approx(0.0175 - 0.005)


# ---------------- exit variants ----------------

def test_no_target_lets_winner_run_until_time():
    o, h, l, c = arrays([(99, 100, 98, 100), (101, 103, 100, 102), (103, 130, 102, 125), (125, 140, 124, 138)])
    fi, xi, fpx, xpx, why = simulate_trade(o, h, l, c, 0, plan(), 5, 3, exits={"target_mode": "none"})
    assert why == "time" and xpx == 138


def test_breakeven_stop_moves_after_1r():
    # fill 101, stop 95 -> risk 6; bar 2 high 108 >= 101 + 6 -> stop to 101 from bar 3
    o, h, l, c = arrays([(99, 100, 98, 100), (101, 103, 100, 102), (103, 108, 102, 104), (103, 104, 100, 101)])
    *_, xpx, why = simulate_trade(o, h, l, c, 0, plan(), 5, 30, exits={"breakeven_at_r": 1.0})
    assert why == "trail_stop" and xpx == 101


def test_atr_trail_applies_from_next_bar():
    aux = {"atr": np.full(5, 2.0)}
    o, h, l, c = arrays([(99, 100, 98, 100), (101, 103, 100, 102), (103, 111, 102, 110),
                         (109, 109, 105, 106), (106, 107, 103, 104)])
    # peak close 110 at bar 2 -> trail 110 - 3*2 = 104, active from bar 3; bar 3 low 105 ok; bar 4 low 103 hits
    *_, xi, _, xpx, why = simulate_trade(o, h, l, c, 0, plan(), 5, 30,
                                         exits={"target_mode": "none", "trail_atr_mult": 3.0}, aux=aux)
    assert why == "trail_stop" and xpx == 104


def test_ma_exit_next_open():
    aux = {"ema21": np.array([0, 0, 0, 105.0, 0])}
    o, h, l, c = arrays([(99, 100, 98, 100), (101, 103, 100, 102), (103, 106, 102, 105),
                         (105, 105, 103, 104), (103.5, 104, 102, 103)])
    fi, xi, fpx, xpx, why = simulate_trade(o, h, l, c, 0, plan(), 5, 30,
                                           exits={"target_mode": "none", "ma_exit": "ema21"}, aux=aux)
    assert why == "ma_exit" and xi == 4 and xpx == 103.5


def test_confirm_close_entry_needs_volume():
    p = plan("pre_breakout", stop=97, cancel=90)
    o, h, l, c = arrays([(96, 97, 95, 96), (99, 102, 98, 101), (101.5, 103, 101, 102), (102, 103, 101, 102)])
    quiet = {"rvol": np.array([1.0, 1.0, 1.0, 1.0])}
    loud = {"rvol": np.array([1.0, 2.0, 1.0, 1.0])}
    ex = {"pre_breakout_entry": "confirm_close"}
    assert simulate_trade(o, h, l, c, 0, p, 3, 30, exits=ex, aux=quiet) is None
    fi, _, fpx, *_ = simulate_trade(o, h, l, c, 0, p, 3, 30, exits=ex, aux=loud)
    assert fi == 2 and fpx == 101.5                       # bought the open after the confirming close
