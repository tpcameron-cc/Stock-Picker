from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from scanners.political.persistence import compare, formation_dates, forward_excess, screen_at, walk_forward

IDX = pd.bdate_range("2025-01-01", periods=600)


@pytest.fixture
def S():
    return load_settings(DEFAULT_SETTINGS).section("political.screen")


def series(vals):
    return pd.Series(np.asarray(vals, float), index=IDX[: len(vals)])


def tx(*rows):
    cols = ["bioguide", "ticker", "type", "trade_date", "filing_date", "amount_low", "amount_high"]
    t = pd.DataFrame(rows, columns=cols)
    t["asset_type"], t["filing_notes"] = "ST", ""
    return t


def test_forward_excess_enters_after_filing_and_needs_full_horizon():
    close = series([10] * 11 + [20] * 589)          # jumps on session 11
    spy = series([100] * 600)
    # filed on session 10 -> entry at session 11's close (20): the jump is NOT captured
    fx, st = forward_excess(close, spy, IDX[10].date(), 5)
    assert st == "ok" and fx == pytest.approx(0.0)
    # filed on session 9 -> entry at session 10 (10) -> +100%
    fx, _ = forward_excess(close, spy, IDX[9].date(), 5)
    assert fx == pytest.approx(1.0)
    assert forward_excess(close, spy, IDX[590].date(), 126)[1] == "horizon incomplete"


def test_screen_at_uses_only_trades_filed_by_formation(S):
    px = {"X": series(np.linspace(10, 20, 600)), "Y": series([10] * 600)}
    spy = series([100] * 600)
    t = tx(("A", "X", "P", date(2025, 3, 3), date(2025, 3, 10), 1001, 15000),
           ("A", "Y", "P", date(2026, 1, 5), date(2026, 2, 20), 1001, 15000))   # traded before F, filed after
    F = date(2026, 1, 15)
    f = screen_at(t, px, spy, F, S).set_index("bioguide")
    assert f.loc["A", "n_trades"] == 1 and f.loc["A", "excess_1y"] > 0
    f2 = screen_at(t, px, spy, date(2026, 3, 1), S).set_index("bioguide")
    assert f2.loc["A", "n_trades"] == 2


def test_compare_detects_persistence_and_guards_small_samples():
    rng = np.random.default_rng(1)
    rows = []
    for m in range(8):
        for k in range(5):
            rows.append({"bioguide": f"W{m}", "active": True, "beat": True, "fwd_excess": 0.10 + rng.normal(0, 0.02)})
            rows.append({"bioguide": f"L{m}", "active": True, "beat": False, "fwd_excess": -0.05 + rng.normal(0, 0.02)})
    obs = pd.DataFrame(rows)
    r = compare(obs, "active", "beat", 2000, 0.9, 5)
    assert r["diff"] == pytest.approx(0.15, abs=0.02) and r["ci_low"] > 0 and "beat the others" in r["verdict"]
    r = compare(obs[obs["bioguide"].isin(["W0", "W1", "L0", "L1", "L2", "L3", "L4", "L5"])], "active", "beat", 500, 0.9, 5)
    assert "too few members" in r["verdict"]
    noisy = obs.assign(fwd_excess=rng.normal(0, 0.2, len(obs)))
    assert "no detectable difference" in compare(noisy, "active", "beat", 2000, 0.9, 5)["verdict"]


def test_walk_forward_labels_next_quarter_buys_with_formation_flags(S):
    up, down = series(np.linspace(10, 30, 600)), series(np.linspace(30, 10, 600))
    px = {"UP": up, "DN": down, "NEW": up}
    spy = series([100] * 600)
    t = tx(("A", "UP", "P", date(2025, 2, 3), date(2025, 2, 10), 1001, 15000),     # A beat SPY in the window
           ("B", "DN", "P", date(2025, 2, 3), date(2025, 2, 10), 1001, 15000),     # B trailed
           ("A", "NEW", "P", date(2026, 2, 2), date(2026, 2, 9), 1001, 15000),     # next quarter's buys
           ("B", "NEW", "P", date(2026, 2, 2), date(2026, 2, 9), 1001, 15000))
    dates = formation_dates(date(2025, 1, 1), date(2026, 3, 1), 365, 91)
    assert dates[0] == date(2026, 1, 1)
    form, obs = walk_forward(t, px, spy, S, dates, 91, 20)
    o = obs.set_index("bioguide")
    assert len(obs) == 2 and bool(o.loc["A", "beat"]) and not bool(o.loc["B", "beat"])
    assert (obs["status"] == "ok").all() and (obs["fwd_excess"] > 0).all()
