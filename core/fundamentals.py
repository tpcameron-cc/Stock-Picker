"""Fundamental checks shared by skills (growth, deceleration, estimate revisions, earnings proximity).

Pure functions over the raw dict returned by a provider's `fundamentals()`; no I/O.
Missing data yields `passed=None` ("unknown") plus a note, never a guessed value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd


@dataclass
class FundamentalResult:
    passed: bool | None                     # True pass, False fail, None = insufficient data
    fail_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # data gaps and context
    metrics: dict[str, float | str | None] = field(default_factory=dict)
    next_earnings: date | None = None
    trading_days_to_earnings: int | None = None


def yoy_growth(s: pd.Series, offset: int = 0) -> float | None:
    """Growth of the quarter `offset` back from the latest vs the same quarter a year earlier.
    Uses abs() of the base so a loss shrinking counts as positive growth. None if not enough data."""
    s = s.dropna()
    i_new, i_old = len(s) - 1 - offset, len(s) - 5 - offset
    if i_old < 0:
        return None
    new, old = float(s.iloc[i_new]), float(s.iloc[i_old])
    if old == 0:
        return None
    return (new - old) / abs(old)


def trading_days_between(start: date, end: date) -> int:
    """Business days after `start` up to and including `end` (tomorrow = 1). Ignores exchange holidays."""
    return int(np.busday_count(start + timedelta(days=1), end + timedelta(days=1)))


def _growth_check(r: FundamentalResult, checks: list[bool], label: str, s: pd.Series | None,
                  max_decel_pp: float, strong_growth_floor: float) -> None:
    """YoY growth must be positive; deceleration > max_decel_pp fails unless growth stays >= floor."""
    if s is None or len(s.dropna()) < 5:
        r.notes.append(f"{label} YoY unavailable")
        return
    g, g_prev = yoy_growth(s), yoy_growth(s, 1)
    key = label.lower()
    r.metrics[f"{key}_yoy"] = g
    if g is None:
        r.notes.append(f"{label} YoY undefined (zero base)")
        return
    checks.append(g > 0)
    if g <= 0:
        r.fail_reasons.append(f"{label} YoY {g:+.0%}")
    if float(s.dropna().iloc[-1]) <= 0:
        r.notes.append(f"latest {label} is negative")
    if g_prev is None:
        r.notes.append(f"{label} deceleration unknown (needs 6 quarters)")
        return
    r.metrics[f"{key}_yoy_prev"] = g_prev
    if (g_prev - g) * 100 > max_decel_pp:
        if g < strong_growth_floor:
            checks.append(False)
            r.fail_reasons.append(f"{label} growth slowing sharply ({g_prev:+.0%} -> {g:+.0%})")
        else:
            r.notes.append(f"{label} growth decelerating ({g_prev:+.0%} -> {g:+.0%}) but still >= {strong_growth_floor:.0%}")


def evaluate_fundamentals(raw: dict, today: date, max_decel_pp: float = 25,
                          revision_tolerance: float = 0.02, strong_growth_floor: float = 0.5) -> FundamentalResult:
    r = FundamentalResult(passed=None)
    for e in raw.get("errors", []):
        r.notes.append(f"unavailable: {e}")

    checks: list[bool] = []
    _growth_check(r, checks, "Revenue", raw.get("revenue"), max_decel_pp, strong_growth_floor)
    _growth_check(r, checks, "EPS", raw.get("eps_reported"), max_decel_pp, strong_growth_floor)

    # Estimate revisions: 0q and +1q. Falling = more down than up revisions in 30d, or consensus down > tol.
    rev_df, trend = raw.get("eps_revisions"), raw.get("eps_trend")
    seen_revisions = False
    for period in ("0q", "+1q"):
        if rev_df is not None and period in getattr(rev_df, "index", []):
            up, down = rev_df.loc[period].get("upLast30days"), rev_df.loc[period].get("downLast30days")
            if pd.notna(up) and pd.notna(down):
                seen_revisions = True
                r.metrics[f"revisions_{period}_up_down"] = f"{int(up)}/{int(down)}"
                if down > up:
                    checks.append(False)
                    r.fail_reasons.append(f"{period} EPS revisions falling ({int(up)} up / {int(down)} down, 30d)")
        if trend is not None and period in getattr(trend, "index", []):
            cur, ago = trend.loc[period].get("current"), trend.loc[period].get("30daysAgo")
            if pd.notna(cur) and pd.notna(ago) and ago != 0:
                seen_revisions = True
                chg = (cur - ago) / abs(ago)
                r.metrics[f"eps_est_{period}_30d_chg"] = chg
                if chg < -revision_tolerance:
                    checks.append(False)
                    r.fail_reasons.append(f"{period} EPS estimate down {-chg:.1%} in 30d")
    if not seen_revisions:
        r.notes.append("estimate revisions unavailable")

    # Earnings proximity (reported separately; the caller decides what to do with it).
    ne = raw.get("next_earnings")
    if ne is not None:
        r.next_earnings = ne
        r.trading_days_to_earnings = trading_days_between(today, ne)
    else:
        r.notes.append("next earnings date unknown")

    if r.fail_reasons:
        r.passed = False
    elif checks:
        r.passed = True
    return r
