from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.providers.attention import SnapshotStore, news_velocity
from scanners.hype.logic import attention_score, confirmation, late_stage, make_plan, pump_flags, squeeze_note


@pytest.fixture
def H():
    return load_settings(DEFAULT_SETTINGS).section("hype")


def frame(closes, highs=None, lows=None, vols=None, opens=None):
    closes = np.asarray(closes, float)
    n = len(closes)
    return pd.DataFrame({"open": np.asarray(opens, float) if opens is not None else closes,
                         "high": np.asarray(highs, float) if highs is not None else closes * 1.01,
                         "low": np.asarray(lows, float) if lows is not None else closes * 0.99,
                         "close": closes, "volume": np.asarray(vols, float) if vols is not None else np.full(n, 1e6)},
                        index=pd.bdate_range("2026-06-01", periods=n))


def breakout_day(last_close=110.0, last_vol=3e6, last_low=103.0, last_high=111.0):
    closes = list(np.linspace(90, 104, 79)) + [last_close]
    vols = [1e6] * 79 + [last_vol]
    highs = [c * 1.01 for c in closes[:-1]] + [last_high]
    lows = [c * 0.99 for c in closes[:-1]] + [last_low]
    return frame(closes, highs, lows, vols)


def test_confirmation_passes_and_fails(H):
    fails, d = confirmation(breakout_day(), H)
    assert fails == [] and d["rvol"] == pytest.approx(3.0) and d["close_in_range"] == pytest.approx(7 / 8)
    fails, _ = confirmation(breakout_day(last_vol=1.5e6), H)
    assert any("relative volume" in f for f in fails)
    fails, _ = confirmation(breakout_day(last_close=104.5, last_high=111.0), H)       # faded into the close
    assert any("hold gains" in f for f in fails)
    down = frame(list(np.linspace(110, 90, 80)))
    fails, _ = confirmation(down, H)
    assert any("not above" in f for f in fails) and any("not rising" in f for f in fails)


def test_late_stage(H):
    ok, d = late_stage(breakout_day(), 100, None, 80, H)
    assert ok == [] and d["atrs_above_sma20"] < 3
    rocket = frame([10.0] * 75 + [12, 15, 18, 22, 25])
    reasons, _ = late_stage(rocket, 100, None, 80, H)
    assert any("extended" in r for r in reasons) and any("in 5 sessions" in r for r in reasons)
    fading, _ = late_stage(breakout_day(), 50, 200, 60, H)
    assert any("past its peak" in r for r in fading)
    fading, _ = late_stage(breakout_day(), 50, None, 120, H)
    assert any("fading" in r for r in fading)


def test_attention_score(H):
    a = H["attention"]
    s, p = attention_score(5.0, 5.0, 1, 300, "Bullish", 1.0, a)
    assert s == pytest.approx(100) and p["stocktwits_trending"] == 20
    s, p = attention_score(None, None, None, None, None, None, a)
    assert s == 0
    _, p = attention_score(1.0, 3.25, 31, 300, "Bearish", 0.1, a)
    assert p["reddit_velocity"] == 0 and p["news_velocity"] == pytest.approx(12.5)
    assert p["stocktwits_trending"] == 0 and p["wsb_comments"] == 0 and p["call_volume"] == 0


def test_pump_flags_and_squeeze(H):
    f = pump_flags(12, 150e6, ["reverse split 1-for-10 on 2026-06-01", "EDGAR check unavailable (x)"], H)
    assert len(f) == 3 and any("r/pennystocks" in x for x in f) and any("micro cap" in x for x in f)
    assert pump_flags(0, 5e9, [], H) == []
    hot = squeeze_note({"short_pct_float": 0.3, "days_to_cover": 6, "short_interest_date": "2026-10-30"}, H)
    assert hot.startswith("squeeze potential") and "borrow fee unavailable" in hot
    assert not squeeze_note({"short_pct_float": 0.05, "days_to_cover": 1}, H).startswith("squeeze")
    assert squeeze_note(None, H) is None


def test_plan(H):
    df = breakout_day()
    plan, why = make_plan(df, H, atr=3.0)
    assert why is None and plan.stop == pytest.approx(107.0) and "ATR" in plan.stop_basis   # 110 - 3 > low 103
    mid = (plan.entry_low + plan.entry_high) / 2
    assert plan.targets == pytest.approx([mid + 2 * (mid - 107), mid + 3 * (mid - 107)]) and plan.horizon_days == 5
    plan, _ = make_plan(breakout_day(last_low=106.0), H, atr=8.0)                           # low is the higher stop
    assert plan.stop == 106.0 and plan.stop_basis == "today's low"


def test_news_velocity():
    asof = date(2026, 11, 16)
    counts = {"2026-11-16": 30, "2026-11-15": 10}
    counts.update({f"2026-10-{d:02d}": 2 for d in range(20, 31)})
    v, d = news_velocity({"counts": counts, "items": 62, "capped": False}, asof)
    span = (date(2026, 11, 15) - date(2026, 10, 20)).days
    assert d["news_daily_baseline"] == pytest.approx(round(22 / span, 2)) and v == pytest.approx(20 / (22 / span))
    assert news_velocity(None, asof)[0] is None
    _, d = news_velocity({"counts": counts, "items": 100, "capped": True}, asof)
    assert "capped" in d["news_note"]


def test_snapshot_store(tmp_path):
    s = SnapshotStore(tmp_path)
    s.save(date(2026, 11, 14), {"apewisdom": [{"ticker": "AAA", "mentions": 10}]})
    s.save(date(2026, 11, 15), {"apewisdom": [{"ticker": "AAA", "mentions": 30}, {"ticker": "BBB", "mentions": 5}]})
    s.save(date(2026, 11, 16), {"apewisdom": [{"ticker": "AAA", "mentions": 999}]})            # today: excluded
    h = s.mentions_history(date(2026, 11, 16), 30)
    assert h["date"].nunique() == 2 and h.loc[h["ticker"] == "AAA", "mentions"].sum() == 40
