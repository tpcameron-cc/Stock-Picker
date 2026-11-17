"""Scoring, play selection and trade plans for catalyst-plays. Pure functions, no I/O.

Plays for an upcoming report:
  run_up        buy now, sell before the report (stock tends to rise into earnings)
  hold_through  hold through the report (consistent beats, rising estimates, cheap implied move)
  post_drift    default when the odds aren't clear: wait, and trade the drift only if it beats and gaps up
  avoid         estimates falling into the report
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd


@dataclass
class CatalystPlan:
    play: str                      # run_up | hold_through | post_drift_entry
    setup: str                     # stored in Signal.metrics["setup"]
    entry_low: float
    entry_high: float
    stop: float
    target: float
    target_basis: str
    horizon_days: int
    entry_mode: str = "next_open"
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def revision_state(raw: dict | None, tol: float) -> str | None:
    """'up' | 'flat' | 'down' from the current-quarter EPS estimate's 30-day change and revision counts.
    None if unavailable."""
    if not raw:
        return None
    trend, revs = raw.get("eps_trend"), raw.get("eps_revisions")
    chg = up = down = None
    if trend is not None and "0q" in getattr(trend, "index", []):
        cur, ago = trend.loc["0q"].get("current"), trend.loc["0q"].get("30daysAgo")
        if pd.notna(cur) and pd.notna(ago) and ago != 0:
            chg = (cur - ago) / abs(ago)
    if revs is not None and "0q" in getattr(revs, "index", []):
        up, down = revs.loc["0q"].get("upLast30days"), revs.loc["0q"].get("downLast30days")
        up, down = (None, None) if pd.isna(up) or pd.isna(down) else (up, down)
    if chg is None and up is None:
        return None
    if (chg is not None and chg < -tol) or (up is not None and down > up):
        return "down"
    if (chg is not None and chg > tol) or (up is not None and up > down and (chg is None or chg >= 0)):
        return "up"
    return "flat"


def guidance_proxy(raw: dict | None, min_change: float) -> str | None:
    """Did forward estimates rise after the report? 'raised' | 'cut' | 'flat' | None (unknown).
    Uses current fiscal year (0y) and next quarter (+1q) consensus vs 30 days ago. A PROXY for guidance."""
    trend = (raw or {}).get("eps_trend")
    if trend is None:
        return None
    changes = []
    for period in ("0y", "+1q"):
        if period in getattr(trend, "index", []):
            cur, ago = trend.loc[period].get("current"), trend.loc[period].get("30daysAgo")
            if pd.notna(cur) and pd.notna(ago) and ago != 0:
                changes.append((cur - ago) / abs(ago))
    if not changes:
        return None
    if max(changes) >= min_change:
        return "raised"
    if min(changes) <= -min_change:
        return "cut"
    return "flat"


SQRT_2_OVER_PI = float(np.sqrt(2 / np.pi))   # E|X| / sigma for a normal move; ATM straddle/spot ~ E|move|


def event_implied_move(straddle_move: float, daily_vol: float, sessions_to_expiry: int) -> float | None:
    """Earnings-day move implied by the straddle, with ordinary day-to-day variance removed.

    The straddle prices the whole period to expiry: sigma_total^2 = sigma_event^2 + (n-1) * sigma_daily^2,
    where sigma_total = (straddle/spot) / sqrt(2/pi). Returns the expected absolute event move
    sqrt(2/pi) * sigma_event, or None when the baseline already explains the whole straddle."""
    if straddle_move is None or straddle_move <= 0 or daily_vol is None or np.isnan(daily_vol):
        return None
    sigma_total = straddle_move / SQRT_2_OVER_PI
    var_event = sigma_total ** 2 - max(int(sessions_to_expiry) - 1, 0) * daily_vol ** 2
    if var_event <= 0:
        return None
    return float(np.sqrt(var_event) * SQRT_2_OVER_PI)


def implied_ratio(implied: dict | None, avg_abs_move: float) -> float | None:
    """Earnings-implied move / average actual reaction move. Uses `event_move` (baseline removed)."""
    if not implied or implied.get("event_move") is None:
        return None
    if not avg_abs_move or np.isnan(avg_abs_move) or avg_abs_move <= 0:
        return None
    return implied["event_move"] / avg_abs_move


# ---------------------------------------------------------------------------
# Score and play
# ---------------------------------------------------------------------------

def _scaled(x, lo, hi, pts) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0.0
    return float(np.clip((x - lo) / (hi - lo), 0, 1) * pts)


def score_upcoming(summary: dict, rev: str | None, ratio: float | None, c: dict) -> tuple[float, dict]:
    s, p = c["scoring"], c["play"]
    parts = {
        "beat_history": _scaled(summary.get("beat_rate"), 0.5, 1.0, s["beat_history"]),
        "revisions": {"up": s["revisions"], "flat": s["revisions"] / 2}.get(rev, 0.0),
        "implied_vs_actual": (0.0 if ratio is None else s["implied_vs_actual"] if ratio <= p["cheap_implied_ratio"]
                              else s["implied_vs_actual"] / 2 if ratio <= 1.0 else 0.0),
        "drift_history": _scaled(summary.get("avg_drift_after_beat"), 0.0, 0.05, s["drift_history"]),
        "runup_history": _scaled(summary.get("runup_hit_rate"), 0.5, 1.0, s["runup_history"]),
    }
    return float(sum(parts.values())), parts


def last_exit_day(report_date: date, timing: str) -> date:
    """Last session you can still hold before the report: the report day for after-close reports,
    otherwise the session before (before-open or unknown timing)."""
    if timing == "amc":
        return report_date
    d = pd.Timestamp(report_date) - pd.offsets.BDay(1)
    return d.date()


def sessions_between(start: date, end: date) -> int:
    """Weekdays after `start` up to and including `end` (exchange holidays not excluded)."""
    return int(np.busday_count(start + timedelta(days=1), end + timedelta(days=1)))


def choose_play(summary: dict, rev: str | None, ratio: float | None, sessions_to_exit: int, c: dict
                ) -> tuple[str, list[str]]:
    """Returns (play, reasons). Plays: run_up | runup_watch | hold_through | post_drift | avoid."""
    p = c["play"]
    why: list[str] = []
    if summary.get("n", 0) < int(c["min_history_quarters"]):
        return "post_drift", [f"only {summary.get('n', 0)} usable past reports (< {c['min_history_quarters']})"]
    if rev == "down":
        return "avoid", ["EPS estimates falling into the report"]

    rh, ra = summary.get("runup_hit_rate"), summary.get("avg_runup")
    runup_ok = rh is not None and not np.isnan(rh) and rh >= p["runup_min_hit_rate"] and ra >= p["runup_min_avg"]
    window = int(c["runup_window_days"])
    if runup_ok and int(p["runup_min_days"]) <= sessions_to_exit <= window:
        return "run_up", [f"rose into {rh:.0%} of past reports, avg {ra:+.1%} over {window} sessions"]

    br = summary.get("beat_rate")
    hold_ok = (br is not None and br >= p["hold_min_beat_rate"]
               and (rev == "up" or not p["hold_requires_revisions_up"])
               and ratio is not None and ratio <= p["cheap_implied_ratio"]
               and (summary.get("avg_move_after_beat") or 0) > 0)
    if hold_ok:
        return "hold_through", [f"beat {br:.0%} of reports, estimates rising, implied move {ratio:.2f}x "
                                f"the average actual move (cheap)"]
    if runup_ok and sessions_to_exit > window:
        return "runup_watch", [f"rose into {rh:.0%} of reports (avg {ra:+.1%}); run-up window opens in "
                               f"{sessions_to_exit - window} sessions"]
    if runup_ok:
        why.append(f"run-up pattern but only {sessions_to_exit} sessions left")
    if br is not None and br < p["hold_min_beat_rate"]:
        why.append(f"beat rate {br:.0%} < {p['hold_min_beat_rate']:.0%}")
    if ratio is None:
        why.append("implied move unavailable")
    elif ratio > p["cheap_implied_ratio"]:
        why.append(f"implied move {ratio:.2f}x average actual (not cheap)")
    if rev != "up":
        why.append(f"estimates {rev or 'unknown'}")
    return "post_drift", why or ["odds not clearly favorable"]


def plan_upcoming(play: str, close: float, atr: float, summary: dict, implied: dict | None,
                  sessions_to_exit: int, sessions_to_reaction: int, c: dict) -> tuple[CatalystPlan | None, str | None]:
    p = c["play"]
    z = float(p["entry_zone_pct"])
    lo, hi = close * (1 - z), close * (1 + z)
    mid = (lo + hi) / 2
    if play == "run_up":
        stop = close - float(p["runup_atr_stop_mult"]) * atr
        target = close * (1 + summary["avg_runup"])
        if target <= hi:
            return None, f"average run-up {summary['avg_runup']:+.1%} doesn't clear the entry zone"
        return CatalystPlan("run_up", "earnings_run_up", lo, hi, stop, target, "average past run-up",
                            horizon_days=sessions_to_exit,
                            flags=["exit before the report: sell by the close of the last session before it"]), None
    if play == "hold_through":
        stop = close * (1 - float(p["hold_stop_implied_mult"]) * implied["event_move"])
        target = close * (1 + summary["avg_move_after_beat"])
        if target <= hi:
            return None, f"average post-beat move {summary['avg_move_after_beat']:+.1%} doesn't clear the entry zone"
        return CatalystPlan("hold_through", "earnings_hold_through", lo, hi, stop, target,
                            "average reaction-day move after past beats",
                            horizon_days=sessions_to_reaction + int(p["hold_days_after_report"]),
                            flags=["gap risk: the report can gap through the stop; loss can exceed 1R"]), None
    return None, None


# ---------------------------------------------------------------------------
# Post-earnings drift (already reported)
# ---------------------------------------------------------------------------

def evaluate_drift(ev: dict, surprise_pct: float | None, guidance: str | None, c: dict
                   ) -> tuple[CatalystPlan | None, list[str], float, dict]:
    """Check the drift criteria on one recent report. Returns (plan, reasons_failed, score, parts)."""
    d = c["drift"]
    sc = d["scoring"]
    fails: list[str] = []
    parts = {"beat": 0.0, "gap": 0.0, "rvol": 0.0, "guidance": 0.0, "holding": 0.0}

    if surprise_pct is None or np.isnan(surprise_pct):
        fails.append("EPS surprise unknown")
    elif surprise_pct < float(d["big_beat_pct"]):
        fails.append(f"surprise {surprise_pct:+.1f}% < {d['big_beat_pct']}%")
    else:
        parts["beat"] = _scaled(surprise_pct, float(d["big_beat_pct"]), 3 * float(d["big_beat_pct"]), sc["beat"] / 2) + sc["beat"] / 2
    if ev["gap"] < float(d["min_gap_pct"]):
        fails.append(f"gap {ev['gap']:+.1%} < {float(d['min_gap_pct']):.0%}")
    else:
        parts["gap"] = _scaled(ev["gap"], float(d["min_gap_pct"]), 3 * float(d["min_gap_pct"]), sc["gap"] / 2) + sc["gap"] / 2
    if not (ev["rvol"] >= float(d["min_rvol"])):
        fails.append(f"reaction volume {ev['rvol']:.1f}x < {d['min_rvol']}x")
    else:
        parts["rvol"] = _scaled(ev["rvol"], float(d["min_rvol"]), 2 * float(d["min_rvol"]), sc["rvol"] / 2) + sc["rvol"] / 2
    if guidance == "cut" and d["require_guidance_proxy"]:
        fails.append("forward estimates cut since the report")
    elif guidance == "raised":
        parts["guidance"] = sc["guidance"]
    elif guidance == "flat":
        parts["guidance"] = sc["guidance"] / 2
    if ev["min_low_since"] <= ev["pre_close"]:
        fails.append("gap filled (traded back to the pre-report close)")
    elif int(ev["bars_since"]) < int(d["min_hold_sessions"]):
        fails.append(f"reaction day only: the gap hasn't had {d['min_hold_sessions']} session(s) to prove it holds yet")
    else:
        parts["holding"] = sc["holding"]

    score = float(sum(parts.values()))
    if fails:
        return None, fails, score, parts

    z = float(d["entry_zone_pct"])
    close = ev["last_close"]
    lo, hi = close * (1 - z), close * (1 + z)
    stop = ev["reaction_low"]
    if stop >= lo:
        return None, ["price back at the reaction-day low (no room for a stop)"], score, parts
    if (lo - stop) / lo > float(d["max_stop_pct"]):
        return None, [f"stop at reaction-day low is {(lo - stop) / lo:.1%} below entry (> {float(d['max_stop_pct']):.0%})"], score, parts
    mid = (lo + hi) / 2
    target = mid + float(d["target_r"]) * (mid - stop)
    horizon = max(int(c["drift_window_days"]) - int(ev["bars_since"]), int(d["min_horizon_days"]))
    flags = [] if guidance == "raised" else [f"guidance proxy: {guidance or 'unknown'}"]
    return CatalystPlan("post_drift_entry", "post_earnings_drift", lo, hi, stop, target,
                        f"{float(d['target_r']):g}R (drift has no measured target)", horizon, flags=flags), [], score, parts


# ---------------------------------------------------------------------------
# Negative catalysts
# ---------------------------------------------------------------------------

def lockup_flag(first_trade: date | None, today: date, c: dict) -> str | None:
    n = c["negative"]
    if not first_trade:
        return None
    exp = first_trade + timedelta(days=int(n["lockup_days"]))
    days = (exp - today).days
    if -int(n["lockup_recent_days"]) <= days <= int(n["lockup_warn_days"]):
        return f"estimated IPO lockup expiry {exp} ({int(n['lockup_days'])}d after first trade {first_trade}; convention, not the filed date)"
    return None
