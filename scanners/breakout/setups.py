"""Breakout setup detection and trade plans.

Everything here is CAUSAL: the feature value on day t uses only data up to and including day t.
The live scan reads the last row; the backtest reads every row. Same code path for both.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core import indicators as ind

SETUP_FLAGS = ("squeeze", "vcp", "flat_base", "cup_handle", "rs_new_high", "breakout_day", "pullback")


# ---------------------------------------------------------------------------
# Cup with handle (heuristic, evaluated per bar)
# ---------------------------------------------------------------------------

def detect_cup_handle(close: np.ndarray, volume: np.ndarray, p: dict) -> bool:
    """Heuristic cup-with-handle on the trailing window ending at the last element.

    right lip = highest close 'handle_min..handle_max' bars back; handle = bars after it.
    left lip  = highest close 'cup_min..cup_max' bars before the right lip.
    Checks: cup depth in range, right lip recovers to within lip tolerance of the left lip,
    bottom strictly inside the cup, handle shallow and in the upper half of the cup, handle on
    lighter average volume than the cup.
    """
    hmin, hmax = int(p["handle_min_days"]), int(p["handle_max_days"])
    cmin, cmax = int(p["cup_min_days"]), int(p["cup_max_days"])
    n = len(close)
    if n < cmax + hmax + 1:
        return False
    # Right lip: highest close in the window [n-1-hmax, n-1-hmin]
    r_lo, r_hi = n - 1 - hmax, n - 1 - hmin
    ri = r_lo + int(np.argmax(close[r_lo : r_hi + 1]))
    right = close[ri]
    # Left lip: highest close in [ri-cmax, ri-cmin]
    l_lo, l_hi = ri - cmax, ri - cmin
    if l_lo < 0:
        return False
    li = l_lo + int(np.argmax(close[l_lo : l_hi + 1]))
    left = close[li]
    cup = close[li : ri + 1]
    bi = int(np.argmin(cup))
    bottom = cup[bi]
    if bi in (0, len(cup) - 1):
        return False
    depth = 1 - bottom / left
    if not (float(p["cup_min_depth"]) <= depth <= float(p["cup_max_depth"])):
        return False
    if right < left * (1 - float(p["cup_lip_tolerance"])):
        return False
    handle = close[ri + 1 :]
    if len(handle) == 0:
        return False
    h_low = handle.min()
    if 1 - h_low / right > float(p["handle_max_depth"]):
        return False
    if h_low < bottom + float(p["handle_min_cup_position"]) * (right - bottom):  # handle in upper part of cup
        return False
    if volume[ri + 1 :].mean() >= volume[li : ri + 1].mean():
        return False
    return True


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def compute_features(df: pd.DataFrame, bench_close: pd.Series, b: dict, atr_period: int = 14,
                     rs_lookback: int = 63) -> pd.DataFrame:
    """Per-day features for one ticker. `b` is the `breakout` settings section."""
    base, bd, pb = b["base"], b["breakout_day"], b["pullback"]
    c, h, l = df["close"], df["high"], df["low"]
    f = pd.DataFrame(index=df.index)
    f["open"], f["high"], f["low"], f["close"] = df["open"], h, l, c

    f["sma50"], f["sma150"], f["sma200"] = ind.sma(c, 50), ind.sma(c, 150), ind.sma(c, 200)
    f["ema21"] = ind.ema(c, 21)
    f["atr"] = ind.atr(df, atr_period)
    f["ma_stack"] = ind.ma_stack(c, (50, 150, 200)) & (c > f["sma50"])
    f["high_252"] = h.rolling(252, min_periods=1).max()
    f["pct_from_high"] = c / f["high_252"] - 1

    # Base / pivot over the prior `length_days` bars (excludes today, so a breakout day can clear it)
    L = int(base["length_days"])
    f["pivot"] = h.shift(1).rolling(L, min_periods=L).max()
    f["base_low"] = l.shift(1).rolling(L, min_periods=L).min()
    f["base_depth"] = f["pivot"] / f["base_low"] - 1
    f["flat_base"] = f["base_depth"] <= float(base["flat_max_depth"])

    # Squeeze: Bollinger bandwidth in its lowest X percentile of 126 days, or ATR contraction
    bw = ind.bollinger(c, 20, 2.0)["bandwidth"]
    bw_pct = bw.rolling(126, min_periods=60).rank(pct=True) * 100
    vc = ind.volatility_contraction(df, atr_period, 60)
    f["squeeze"] = (bw_pct <= float(base["squeeze_bw_percentile"])) | (vc <= float(base["squeeze_atr_ratio"]))

    # VCP: three consecutive segments, each shallower than the last
    n = int(base["vcp_segment_days"])

    def seg_depth(shift: int) -> pd.Series:
        hh = h.shift(shift).rolling(n, min_periods=n).max()
        ll = l.shift(shift).rolling(n, min_periods=n).min()
        return 1 - ll / hh

    d_old, d_mid, d_new = seg_depth(2 * n), seg_depth(n), seg_depth(0)
    f["vcp"] = (d_old > d_mid) & (d_mid > d_new) & (d_new <= float(base["vcp_max_final_depth"]))

    # Breakout day: close above pivot on volume, closing in the upper part of the range
    f["rvol"] = ind.relative_volume(df, 50)
    rng = (h - l).replace(0, np.nan)
    close_pos = (c - l) / rng
    f["breakout_day"] = (c > f["pivot"]) & (f["rvol"] >= float(bd["rvol_min"])) & (
        close_pos >= float(bd["close_in_range_min"]))

    # Relative strength vs benchmark
    bc = bench_close.reindex(df.index).ffill()
    f["rs_ret"] = c / c.shift(rs_lookback) - bc / bc.shift(rs_lookback)
    rsl = c / bc
    f["rs_new_high"] = rsl >= rsl.rolling(252, min_periods=126).max() * (1 - float(b["rs_new_high_tolerance"]))

    # Pullback to a rising EMA21 or SMA50
    lb, tol = int(pb["ma_rising_lookback"]), float(pb["ma_touch_tolerance"])

    def touch(ma: pd.Series) -> pd.Series:
        return (ma > ma.shift(lb)) & (l <= ma * (1 + tol)) & (c >= ma)

    t21, t50 = touch(f["ema21"]), touch(f["sma50"])
    f["pullback_ma"] = np.where(t21, "ema21", np.where(t50, "sma50", ""))
    f["pullback"] = t21 | t50

    # Cup with handle: expensive, so only evaluate where it could matter (leaders near highs)
    f["cup_handle"] = False
    window = int(base["cup_max_days"]) + int(base["handle_max_days"]) + 1
    eligible = f["ma_stack"] & (f["pct_from_high"] >= -float(base["near_high_max_pct"]))
    closes, vols = c.to_numpy(float), df["volume"].to_numpy(float)
    for i in np.flatnonzero(eligible.to_numpy()):
        if i + 1 >= window:
            f.iat[i, f.columns.get_loc("cup_handle")] = detect_cup_handle(
                closes[i + 1 - window : i + 1], vols[i + 1 - window : i + 1], base)
    for col in SETUP_FLAGS + ("ma_stack",):
        f[col] = f[col].fillna(False).astype(bool)
    return f


# ---------------------------------------------------------------------------
# Gates, plan, score
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    setup: str                       # breakout | pre_breakout | pullback
    entry_low: float
    entry_high: float
    stop: float
    target: float
    target_basis: str
    pivot: float | None
    distance_to_pivot: float | None  # pivot / close - 1 (negative = above pivot)
    hits: list[str] = field(default_factory=list)
    cancel_below: float | None = None  # pending buy-stop is cancelled if price undercuts this (base low)


def passes_gates(row: pd.Series, rs_pct: float, b: dict) -> list[str]:
    """Return gate failures (empty list = passes)."""
    fails = []
    if b.get("require_ma_stack", True) and not row["ma_stack"]:
        fails.append("no 50>150>200 MA stack / close below SMA50")
    if not (rs_pct >= float(b["rs_min_percentile"])):
        fails.append(f"RS percentile {rs_pct:.0f} < {b['rs_min_percentile']}")
    if not (row["pct_from_high"] >= -float(b["near_high_max_pct"])):
        fails.append(f"{row['pct_from_high']:.0%} from 52w high")
    return fails


def make_plan(row: pd.Series, b: dict) -> tuple[Plan | None, str | None]:
    """Classify the setup and build entry/stop/target. Returns (plan, None) or (None, reject_reason).
    (None, None) means no setup at all."""
    base, p = b["base"], b["plan"]
    bz = float(p["buy_zone_pct"])
    close, pivot, base_low, atr = row["close"], row["pivot"], row["base_low"], row["atr"]
    hits = [k for k in SETUP_FLAGS if row[k]]
    base_hits = [k for k in ("flat_base", "vcp", "cup_handle") if row[k]]
    near_base_high = row["pct_from_high"] >= -float(base["near_high_max_pct"])
    fallback_r = float(p["fallback_target_r"])

    if any(pd.isna(x) for x in (close, pivot, base_low, atr)):
        return None, None

    def base_plan(setup: str) -> tuple[Plan | None, str | None]:
        entry_low, entry_high = pivot, pivot * (1 + bz)
        stop = max(base_low, entry_low - float(p["atr_stop_mult"]) * atr)
        stop_pct = (entry_low - stop) / entry_low
        if stop_pct > float(p["max_stop_pct"]):
            return None, f"{setup}: stop {stop_pct:.1%} below entry (> {float(p['max_stop_pct']):.0%})"
        target, basis = pivot + (pivot - base_low), "measured move (base depth)"
        mid = (entry_low + entry_high) / 2
        if target <= entry_high:
            target, basis = mid + fallback_r * (mid - stop), f"{fallback_r:g}R (base too shallow for measured move)"
        return Plan(setup, entry_low, entry_high, stop, target, basis, pivot, pivot / close - 1, hits,
                    cancel_below=base_low), None

    if row["breakout_day"]:
        if close > pivot * (1 + bz):
            return None, f"extended: close {close / pivot - 1:.1%} above pivot (> {bz:.0%} buy zone)"
        return base_plan("breakout")

    if base_hits and near_base_high and close <= pivot:
        dist = pivot / close - 1
        if dist > float(p["max_distance_to_pivot"]):
            return None, f"pre-breakout {dist:.1%} below pivot (> {float(p['max_distance_to_pivot']):.0%})"
        return base_plan("pre_breakout")

    if row["pullback"]:
        ma = row[row["pullback_ma"]]
        entry_low = ma
        entry_high = max(close, ma * (1 + float(b["pullback"]["ma_touch_tolerance"])))
        stop = ma - float(p["pullback_atr_stop_mult"]) * atr
        stop_pct = (entry_low - stop) / entry_low
        if stop_pct > float(p["max_stop_pct"]):
            return None, f"pullback: stop {stop_pct:.1%} below entry (> {float(p['max_stop_pct']):.0%})"
        mid = (entry_low + entry_high) / 2
        target, basis = row["high_252"], "prior 52-week high"
        if target <= entry_high:
            target, basis = mid + fallback_r * (mid - stop), f"{fallback_r:g}R (at highs, no overhead level)"
        return Plan("pullback", entry_low, entry_high, stop, target, basis, None, None, hits), None

    return None, None


def technical_score(row: pd.Series, rs_pct: float, b: dict) -> float:
    s = b["scoring"]
    pts = 0.0
    pts += s["ma_stack"] * bool(row["ma_stack"])
    lo = float(b["rs_min_percentile"])
    if rs_pct >= lo:
        pts += s["rs_percentile_max"] * (rs_pct - lo) / max(100 - lo, 1e-9)
    for k in SETUP_FLAGS:
        pts += s[k] * bool(row[k])
    return float(pts)
