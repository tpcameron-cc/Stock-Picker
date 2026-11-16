from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.providers.earnings_calendar import parse_day
from core.providers.yfinance_provider import straddle_from_chain
from scanners.catalyst.history import event_stats, history_table, reaction_index, summarize_history
from scanners.catalyst.plays import (
    choose_play, evaluate_drift, guidance_proxy, implied_ratio, last_exit_day, lockup_flag, plan_upcoming,
    revision_state, score_upcoming, sessions_between,
)


@pytest.fixture
def c():
    return load_settings(DEFAULT_SETTINGS).section("catalyst")


def bars(closes, start="2026-01-05", opens=None, lows=None, vol=1e6):
    closes = np.asarray(closes, float)
    o = np.asarray(opens, float) if opens is not None else closes
    lo = np.asarray(lows, float) if lows is not None else np.minimum(o, closes) * 0.99
    v = np.full(len(closes), vol) if np.isscalar(vol) else np.asarray(vol, float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, closes) * 1.01, "low": lo, "close": closes, "volume": v},
                        index=pd.bdate_range(start, periods=len(closes)))


# ---------------- reaction timing ----------------

def test_reaction_index_bmo_vs_amc():
    idx = pd.bdate_range("2026-01-05", periods=10)                 # Mon Jan 5 ...
    assert idx[reaction_index(idx, date(2026, 1, 7), "bmo")].date() == date(2026, 1, 7)    # same day
    assert idx[reaction_index(idx, date(2026, 1, 7), "amc")].date() == date(2026, 1, 8)    # next session
    assert idx[reaction_index(idx, date(2026, 1, 10), "bmo")].date() == date(2026, 1, 12)  # Saturday -> Monday
    assert reaction_index(idx, date(2026, 3, 1), "amc") is None


def test_event_stats_math():
    closes = [100] * 12 + [110, 112, 111]
    opens = [100] * 12 + [106, 111, 111]
    df = bars(closes, opens=opens, lows=[99] * 12 + [105, 110, 110])
    d = df.index[11].date()                                        # amc on bar 11 -> reaction bar 12
    ev = event_stats(df, d, "amc", runup_days=10, drift_days=2)
    assert ev["reaction_date"] == df.index[12].date()
    assert ev["gap"] == pytest.approx(0.06) and ev["move"] == pytest.approx(0.10)
    assert ev["runup"] == pytest.approx(0.0)
    assert ev["drift"] == pytest.approx(111 / 110 - 1)
    assert ev["bars_since"] == 2 and ev["min_low_since"] == 105 and ev["pre_close"] == 100


def test_history_summary():
    # 4 quarterly reports, 3 beats; stock rises into each report then pops
    closes, events = [], []
    for q in range(4):
        closes += list(np.linspace(100, 105, 30))                   # run-up into the report
        closes += [110 if q != 2 else 98] + [111] * 29
    df = bars(closes, start="2024-01-01")
    for q in range(4):
        events.append({"date": df.index[q * 60 + 29].date(), "timing": "amc", "eps_reported": 1.0,
                       "surprise_pct": 5.0 if q != 2 else -3.0})
    t = history_table(df, pd.DataFrame(events), runup_days=10, drift_days=20, quarters=12)
    s = summarize_history(t)
    assert s["n"] == 4 and s["beat_rate"] == 0.75
    assert s["runup_hit_rate"] == 1.0 and s["avg_runup"] > 0
    assert s["avg_move_after_beat"] == pytest.approx(110 / 105 - 1)
    assert summarize_history(pd.DataFrame()) == {"n": 0}


# ---------------- estimates ----------------

def trend(cur0=1.0, ago0=1.0, cur_y=4.0, ago_y=4.0, cur1=1.1, ago1=1.1):
    return pd.DataFrame({"current": [cur0, cur1, cur_y], "30daysAgo": [ago0, ago1, ago_y]}, index=["0q", "+1q", "0y"])


def revs(up=2, down=0):
    return pd.DataFrame({"upLast30days": [up], "downLast30days": [down]}, index=["0q"])


def test_revision_state(c):
    tol = c["revisions_tolerance"]
    assert revision_state({"eps_trend": trend(1.05, 1.0), "eps_revisions": revs(3, 0)}, tol) == "up"
    assert revision_state({"eps_trend": trend(0.95, 1.0), "eps_revisions": revs(3, 0)}, tol) == "down"
    assert revision_state({"eps_trend": trend(1.0, 1.0), "eps_revisions": revs(1, 4)}, tol) == "down"
    assert revision_state({"eps_trend": trend(1.0, 1.0), "eps_revisions": revs(0, 0)}, tol) == "flat"
    assert revision_state({}, tol) is None


def test_guidance_proxy():
    assert guidance_proxy({"eps_trend": trend(cur_y=4.2, ago_y=4.0)}, 0.01) == "raised"
    assert guidance_proxy({"eps_trend": trend(cur1=1.0, ago1=1.1)}, 0.01) == "cut"
    assert guidance_proxy({"eps_trend": trend()}, 0.01) == "flat"
    assert guidance_proxy(None, 0.01) is None


# ---------------- options ----------------

def chain(strikes, bid, ask, last=None):
    return pd.DataFrame({"strike": strikes, "bid": bid, "ask": ask, "lastPrice": last or [0] * len(strikes)})


def test_straddle_from_chain():
    calls = chain([95, 100, 105], [6, 3, 1], [6.4, 3.2, 1.2])
    puts = chain([95, 100, 105], [1, 2.8, 6], [1.2, 3.0, 6.4])
    r = straddle_from_chain(calls, puts, 100.4, date(2026, 11, 20))
    assert r["strike"] == 100 and r["straddle_mid"] == pytest.approx(3.1 + 2.9)
    assert r["move"] == pytest.approx(6.0 / 100.4) and not r["used_last"]
    assert r["spread_pct"] == pytest.approx((0.2 + 0.2) / 6.0)
    stale = straddle_from_chain(chain([100], [0], [0], [3.0]), chain([100], [0], [0], [2.0]), 100, None)
    assert stale["used_last"] and stale["move"] == pytest.approx(0.05)
    assert straddle_from_chain(chain([100], [0], [0]), chain([100], [0], [0]), 100, None) is None


def test_implied_ratio():
    assert implied_ratio({"move": 0.09, "event_move": 0.04}, 0.05) == pytest.approx(0.8)
    assert implied_ratio(None, 0.05) is None
    assert implied_ratio({"move": 0.04, "event_move": 0.04}, float("nan")) is None
    assert implied_ratio({"move": 0.04, "event_move": None}, 0.05) is None


def test_event_implied_move():
    from scanners.catalyst.plays import SQRT_2_OVER_PI, event_implied_move
    # Expiry is the event day itself (1 session): nothing to remove
    assert event_implied_move(0.05, 0.02, 1) == pytest.approx(0.05)
    # Event sigma 6%, daily 2%, 10 sessions to expiry: straddle prices sqrt(0.06^2 + 9*0.02^2)
    total = np.sqrt(0.06 ** 2 + 9 * 0.02 ** 2) * SQRT_2_OVER_PI
    assert event_implied_move(total, 0.02, 10) == pytest.approx(0.06 * SQRT_2_OVER_PI)
    # Baseline explains the whole straddle -> not separable
    assert event_implied_move(0.02, 0.02, 20) is None
    assert event_implied_move(0.05, float("nan"), 5) is None


# ---------------- plays ----------------

def summary(**over):
    s = {"n": 12, "beat_rate": 0.9, "avg_abs_move": 0.06, "avg_move_after_beat": 0.05, "runup_hit_rate": 0.5,
         "avg_runup": 0.0, "avg_drift_after_beat": 0.02}
    s.update(over)
    return s


def test_choose_play(c):
    assert choose_play(summary(), "up", 0.7, 8, c)[0] == "hold_through"
    assert choose_play(summary(), "up", 1.1, 8, c)[0] == "post_drift"                 # implied not cheap
    assert choose_play(summary(), "flat", 0.7, 8, c)[0] == "post_drift"               # revisions not up
    assert choose_play(summary(), "down", 0.7, 8, c)[0] == "avoid"
    assert choose_play(summary(n=3), "up", 0.7, 8, c)[0] == "post_drift"              # too little history
    run = summary(runup_hit_rate=0.8, avg_runup=0.03, beat_rate=0.6)
    assert choose_play(run, "flat", None, 8, c)[0] == "run_up"
    assert choose_play(run, "flat", None, 14, c)[0] == "runup_watch"                  # window not open yet
    assert choose_play(run, "flat", None, 2, c)[0] == "post_drift"                    # too late


def test_score_bounds(c):
    best, parts = score_upcoming(summary(beat_rate=1.0, avg_drift_after_beat=0.06, runup_hit_rate=1.0), "up", 0.5, c)
    assert best == pytest.approx(100)
    worst, _ = score_upcoming({"n": 0}, None, None, c)
    assert worst == 0


def test_plans_are_coherent(c):
    p, err = plan_upcoming("run_up", 100, 2.0, summary(avg_runup=0.04), None, 6, 7, c)
    assert err is None and p.stop == pytest.approx(97) and p.target == pytest.approx(104)
    assert p.entry_low < 100 < p.entry_high < p.target and p.horizon_days == 6
    p, err = plan_upcoming("hold_through", 100, 2.0, summary(), {"move": 0.07, "event_move": 0.04}, 6, 7, c)
    assert p.stop == pytest.approx(96) and p.target == pytest.approx(105) and p.horizon_days == 10
    assert any("gap risk" in f for f in p.flags)
    p, err = plan_upcoming("run_up", 100, 2.0, summary(avg_runup=0.005), None, 6, 7, c)
    assert p is None and "doesn't clear" in err


def test_exit_day_and_sessions():
    assert last_exit_day(date(2026, 11, 19), "amc") == date(2026, 11, 19)
    assert last_exit_day(date(2026, 11, 19), "bmo") == date(2026, 11, 18)
    assert last_exit_day(date(2026, 11, 23), "unknown") == date(2026, 11, 20)     # Monday -> Friday
    assert sessions_between(date(2026, 11, 13), date(2026, 11, 18)) == 3


# ---------------- drift ----------------

def ev(**over):
    e = {"gap": 0.06, "rvol": 3.0, "pre_close": 100.0, "reaction_low": 104.0, "last_close": 110.0,
         "min_low_since": 104.0, "bars_since": 4}
    e.update(over)
    return e


def test_drift_qualifies(c):
    plan, fails, score, parts = evaluate_drift(ev(), 12.0, "raised", c)
    assert fails == [] and plan.setup == "post_earnings_drift"
    assert plan.stop == 104 and plan.entry_low == pytest.approx(107.8)
    mid = (plan.entry_low + plan.entry_high) / 2
    assert plan.target == pytest.approx(mid + 2 * (mid - 104))
    assert plan.horizon_days == 16 and score > 80


@pytest.mark.parametrize("over,surprise,guid,msg", [
    ({}, 2.0, "raised", "surprise"),
    ({"gap": 0.01}, 12.0, "raised", "gap"),
    ({"rvol": 1.2}, 12.0, "raised", "volume"),
    ({"min_low_since": 99.0}, 12.0, "raised", "gap filled"),
    ({}, 12.0, "cut", "estimates cut"),
    ({"reaction_low": 90.0}, 12.0, "raised", "below entry"),
])
def test_drift_rejections(c, over, surprise, guid, msg):
    plan, fails, *_ = evaluate_drift(ev(**over), surprise, guid, c)
    assert plan is None and any(msg in f for f in fails)


def test_drift_unknown_guidance_allowed_but_flagged(c):
    plan, fails, *_ = evaluate_drift(ev(), 12.0, None, c)
    assert plan is not None and any("guidance proxy: unknown" in f for f in plan.flags)


# ---------------- negatives and calendar ----------------

def test_lockup_flag(c):
    today = date(2026, 11, 16)
    assert "lockup" in lockup_flag(date(2026, 5, 25), today, c)       # expiry 2026-11-21: in 5 days
    assert lockup_flag(date(2026, 1, 1), today, c) is None            # long past
    assert lockup_flag(date(2026, 9, 1), today, c) is None            # far ahead
    assert lockup_flag(None, today, c) is None


def test_parse_calendar_day():
    rows = [{"symbol": "abc", "time": "time-pre-market", "epsForecast": "$1.20", "eps": "", "surprise": "N/A"},
            {"symbol": "XYZ", "time": "time-not-supplied", "epsForecast": "($0.10)", "eps": "$0.05", "surprise": "12"}]
    d = parse_day(rows, date(2026, 11, 18))
    assert list(d["symbol"]) == ["ABC", "XYZ"] and list(d["timing"]) == ["bmo", "unknown"]
    assert d["eps_forecast"].tolist() == [1.2, -0.1] and np.isnan(d["eps"].iloc[0]) and d["surprise_pct"].iloc[1] == 12
    assert parse_day(None, date(2026, 11, 21)).empty


def test_drift_prefilter(c):
    from types import SimpleNamespace

    from scanners.catalyst.scan import drift_prefilter
    base = [100.0] * 60
    vol = [1e6] * 60
    # report on bar 59 after the close: bar 60 gaps +6% on 3x volume and holds
    good = bars(base + [106, 107, 108], opens=base + [106, 106.5, 107.5], lows=[99] * 60 + [105, 106, 107],
                vol=vol + [3e6, 1e6, 1e6])
    d = good.index[59].date()
    assert drift_prefilter(good, SimpleNamespace(date=d, timing="amc", surprise_pct=10.0), c) is None
    assert drift_prefilter(good, SimpleNamespace(date=d, timing="unknown", surprise_pct=None), c) is None
    assert "calendar" in drift_prefilter(good, SimpleNamespace(date=d, timing="amc", surprise_pct=1.0), c)
    flat = bars(base + [100.5, 100, 100], vol=vol + [1e6] * 3)
    msg = drift_prefilter(flat, SimpleNamespace(date=d, timing="unknown", surprise_pct=None), c)
    assert msg.startswith("no qualifying reaction") and "bmo" in msg and "amc" in msg
