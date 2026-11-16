from datetime import date

import pandas as pd
import pytest

from core.fundamentals import evaluate_fundamentals, trading_days_between, yoy_growth

TODAY = date(2026, 11, 16)  # Monday


def q(values):
    return pd.Series(values, index=pd.date_range("2025-01-31", periods=len(values), freq="QE"), dtype=float)


def revisions(up0=3, down0=0, up1=2, down1=0):
    return pd.DataFrame({"upLast30days": [up0, up1], "downLast30days": [down0, down1]}, index=["0q", "+1q"])


def trend(cur0=1.0, ago0=1.0, cur1=1.0, ago1=1.0):
    return pd.DataFrame({"current": [cur0, cur1], "30daysAgo": [ago0, ago1]}, index=["0q", "+1q"])


def raw(**over):
    d = {"errors": [], "revenue": q([100, 105, 110, 115, 120]), "eps_reported": q([1.0, 1.1, 1.2, 1.3, 1.4, 1.5]),
         "eps_revisions": revisions(), "eps_trend": trend(), "next_earnings": date(2027, 2, 1)}
    d.update(over)
    return d


def test_yoy_growth():
    assert yoy_growth(q([100, 0, 0, 0, 120])) == pytest.approx(0.20)
    assert yoy_growth(q([-1.0, 0, 0, 0, -0.5])) == pytest.approx(0.5)   # loss shrinking = positive
    assert yoy_growth(q([1, 2, 3, 4])) is None                          # not enough quarters
    assert yoy_growth(q([0, 1, 1, 1, 1])) is None                       # zero base
    assert yoy_growth(q([100, 110, 0, 0, 120, 99]), offset=1) == pytest.approx(0.20)


def test_trading_days_between():
    assert trading_days_between(TODAY, date(2026, 11, 17)) == 1      # tomorrow
    assert trading_days_between(TODAY, date(2026, 11, 23)) == 5      # next Monday
    assert trading_days_between(TODAY, TODAY) == 0


def test_healthy_company_passes():
    r = evaluate_fundamentals(raw(), TODAY)
    assert r.passed is True and r.fail_reasons == []
    assert r.metrics["revenue_yoy"] == pytest.approx(0.20)
    assert r.trading_days_to_earnings > 5


def test_negative_growth_fails():
    r = evaluate_fundamentals(raw(revenue=q([100, 100, 100, 100, 90])), TODAY)
    assert r.passed is False and any("Revenue YoY" in x for x in r.fail_reasons)


def test_sharp_deceleration_fails_unless_still_strong():
    # EPS YoY: prev (1.0 vs 0.25 -> +300%), latest (1.1 vs 1.0 -> +10%) -> slowing sharply
    eps = q([0.25, 1.0, 1.0, 1.0, 1.0, 1.1])
    r = evaluate_fundamentals(raw(eps_reported=eps), TODAY, max_decel_pp=25, strong_growth_floor=0.5)
    assert r.passed is False and any("slowing sharply" in x for x in r.fail_reasons)
    # prev +300%, latest +150%: decelerating but still above the floor -> note, not a fail
    eps = q([0.25, 0.4, 1.0, 1.0, 1.0, 1.0])
    r = evaluate_fundamentals(raw(eps_reported=eps), TODAY, max_decel_pp=25, strong_growth_floor=0.5)
    assert r.passed is True and any("decelerating" in x for x in r.notes)


def test_falling_revisions_fail():
    r = evaluate_fundamentals(raw(eps_revisions=revisions(up0=1, down0=5)), TODAY)
    assert r.passed is False and any("revisions falling" in x for x in r.fail_reasons)
    r = evaluate_fundamentals(raw(eps_trend=trend(cur1=0.95, ago1=1.0)), TODAY, revision_tolerance=0.02)
    assert r.passed is False and any("estimate down 5.0%" in x for x in r.fail_reasons)


def test_missing_data_is_unknown_not_pass():
    r = evaluate_fundamentals({"errors": ["revenue: HTTPError"]}, TODAY)
    assert r.passed is None
    assert any("unavailable" in n for n in r.notes)
    assert any("earnings date unknown" in n for n in r.notes)


def test_earnings_proximity_reported():
    r = evaluate_fundamentals(raw(next_earnings=date(2026, 11, 17)), TODAY)
    assert r.trading_days_to_earnings == 1
