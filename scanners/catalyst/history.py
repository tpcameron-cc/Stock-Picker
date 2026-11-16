"""Past earnings reactions from daily prices. Pure functions, no I/O.

Reaction day = first session that reflects the report: the report date itself for before-open (bmo)
reports, the next session for after-close (amc) and unknown timing.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from core import indicators as ind


def reaction_index(index: pd.DatetimeIndex, report_date: date, timing: str) -> int | None:
    d = pd.Timestamp(report_date)
    pos = index.searchsorted(d, side="left" if timing == "bmo" else "right")
    return int(pos) if pos < len(index) else None


def event_stats(df: pd.DataFrame, report_date: date, timing: str, runup_days: int, drift_days: int,
                rvol: pd.Series | None = None) -> dict | None:
    """Gap, reaction move, pre-report run-up and post-report drift for one report. None if out of range."""
    idx = df.index
    r = reaction_index(idx, report_date, timing)
    if r is None or r < 1:
        return None
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    pre = r - 1
    out = {
        "reaction_date": idx[r].date(),
        "pre_close": c[pre],
        "gap": o[r] / c[pre] - 1,
        "move": c[r] / c[pre] - 1,
        "reaction_low": l[r],
        "reaction_close": c[r],
        "runup": c[pre] / c[pre - runup_days] - 1 if pre - runup_days >= 0 else np.nan,
        "drift": c[r + drift_days] / c[r] - 1 if r + drift_days < len(c) else np.nan,
        "rvol": float(rvol.iloc[r]) if rvol is not None and pd.notna(rvol.iloc[r]) else np.nan,
        "bars_since": len(c) - 1 - r,
        "min_low_since": float(l[r:].min()),
        "last_close": c[-1],
    }
    return out


def history_table(prices: pd.DataFrame, events: pd.DataFrame, runup_days: int, drift_days: int,
                  quarters: int) -> pd.DataFrame:
    """Stats for the last `quarters` REPORTED events (eps_reported not null) covered by `prices`."""
    past = events[events["eps_reported"].notna()].tail(quarters)
    rvol = ind.relative_volume(prices, 50)
    rows = []
    for e in past.itertuples():
        if pd.Timestamp(e.date) < prices.index[0]:
            continue
        st = event_stats(prices, e.date, e.timing, runup_days, drift_days, rvol)
        if st:
            rows.append({"date": e.date, "timing": e.timing, "surprise_pct": e.surprise_pct, **st})
    return pd.DataFrame(rows)


def summarize_history(t: pd.DataFrame) -> dict:
    """Beat rate, typical move, run-up and drift patterns. NaN-safe; `n` = usable quarters."""
    if t.empty:
        return {"n": 0}
    beats = t[t["surprise_pct"] > 0]
    run = t["runup"].dropna()
    drift_b = beats["drift"].dropna()
    return {
        "n": len(t),
        "beat_rate": float((t["surprise_pct"] > 0).mean()) if t["surprise_pct"].notna().any() else np.nan,
        "avg_surprise_pct": float(t["surprise_pct"].mean()),
        "avg_abs_move": float(t["move"].abs().mean()),
        "avg_move_after_beat": float(beats["move"].mean()) if len(beats) else np.nan,
        "runup_hit_rate": float((run > 0).mean()) if len(run) else np.nan,
        "avg_runup": float(run.mean()) if len(run) else np.nan,
        "drift_hit_after_beat": float((drift_b > 0).mean()) if len(drift_b) else np.nan,
        "avg_drift_after_beat": float(drift_b.mean()) if len(drift_b) else np.nan,
    }
