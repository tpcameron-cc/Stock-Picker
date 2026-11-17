from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.calibrate import Calibrator, blended_percentile, skill_edge
from core.config import DEFAULT_SETTINGS, load_settings
from core.regime import Regime
from core.review import score_check
from scanners.run_all import merge_ideas
from tests.test_run_all import ASOF, ctx, sig


@pytest.fixture
def cfg():
    return load_settings(DEFAULT_SETTINGS)


def journal_rows(skill, scores, days_ago=5, status="pending", r=None):
    d = pd.Timestamp(ASOF) - pd.Timedelta(days=days_ago)
    return pd.DataFrame({"skill": skill, "score": scores, "signal_date": d.date().isoformat(), "status": status,
                         "r_multiple": r if r is not None else [np.nan] * len(scores)})


def test_percentile_prior_then_history_takes_over():
    assert blended_percentile(70, np.array([]), 40, 20) == pytest.approx(50)      # middle of [40, 100]
    assert blended_percentile(30, np.array([]), 40, 20) == 0
    ref = np.arange(60, 80, dtype=float)                                          # 20 past scores, 60..79
    # 70 is mid-history (ECDF 0.525) and mid-range (0.5): equal weights at n = k = 20
    assert blended_percentile(70, ref, 40, 20) == pytest.approx(100 * (0.525 + 0.5) / 2)
    big = np.repeat(ref, 50)                                                      # 1,000 scores: history dominates
    assert blended_percentile(79.5, big, 40, 20) == pytest.approx(100, abs=2)


def test_skill_edge_shrinks_small_samples():
    shrunk, n, raw = skill_edge(pd.Series([1.0] * 10), 30)
    assert n == 10 and raw == 1.0 and shrunk == pytest.approx(0.25)
    assert skill_edge(pd.Series([], dtype=float), 30) == (0.0, 0, pytest.approx(np.nan, nan_ok=True))


def test_same_raw_score_ranks_differently_by_skill_history(cfg):
    # catalyst habitually scores high, breakout low: a 75 is ordinary for catalyst, exceptional for breakout
    t = pd.concat([journal_rows("catalyst-plays", list(np.linspace(70, 95, 40))),
                   journal_rows("breakout-scanner", list(np.linspace(45, 70, 40)))])
    c = Calibrator.from_journal(cfg, t, ASOF)
    cat, cp = c.common("catalyst-plays", 75)
    brk, bp = c.common("breakout-scanner", 75)
    assert brk > cat and bp["history_n"] == 40 and cp["percentile"] < 50 < bp["percentile"]
    ideas = merge_ideas({"catalyst-plays": [sig("CAT", "catalyst-plays", 80)],
                         "breakout-scanner": [sig("BRK", "breakout-scanner", 68)]}, ctx(), _regime(), cfg, ASOF, c)
    assert [i.ticker for i in ideas] == ["BRK", "CAT"]          # the lower raw score ranks first: it's rarer for its skill
    assert all(i.rejected is None for i in ideas)               # admission still on each skill's own scale


def test_today_is_not_in_its_own_reference_and_edge_phases_in(cfg):
    today = journal_rows("breakout-scanner", [99.0] * 30, days_ago=0)
    assert Calibrator.from_journal(cfg, today, ASOF).refs == {}
    closed = journal_rows("political-edge", [50.0] * 30, status="closed", r=[0.6] * 30)
    c = Calibrator.from_journal(cfg, closed, ASOF)
    score, parts = c.common("political-edge", 50)
    # shrunk 0.6 * 30/60 = 0.3R -> 0.3 * 25 = +7.5 points
    assert parts["edge_points"] == pytest.approx(7.5) and parts["edge_trades"] == 30
    assert c.common("breakout-scanner", 50)[1]["edge_points"] == 0


def test_primary_skill_chosen_on_common_scale(cfg):
    t = pd.concat([journal_rows("catalyst-plays", list(np.linspace(80, 99, 50))),
                   journal_rows("breakout-scanner", list(np.linspace(40, 60, 50)))])
    c = Calibrator.from_journal(cfg, t, ASOF)
    i = merge_ideas({"catalyst-plays": [sig("X", "catalyst-plays", 82)],
                     "breakout-scanner": [sig("X", "breakout-scanner", 62)]}, ctx(), _regime(), cfg, ASOF, c)[0]
    assert i.primary.skill == "breakout-scanner" and i.score == pytest.approx(72)   # gate: breakout raw 62 + 10


def test_disabled_falls_back_to_raw(cfg):
    cfg.raw["orchestrator"]["calibration"]["enabled"] = False
    c = Calibrator.from_journal(cfg, None, ASOF)
    assert c.common("catalyst-plays", 77)[0] == 77


def test_review_score_check():
    closed = pd.DataFrame({"skill": "breakout-scanner", "score": np.arange(40, 80, dtype=float),
                           "r_multiple": np.linspace(-1, 2, 40)})
    r = score_check(closed, 30).loc["breakout-scanner"]
    assert r["rho"] == pytest.approx(1.0) and r["verdict"] == "higher scores did better" and r["top_half_r"] > r["bottom_half_r"]


def _regime():
    return Regime("strong", 6, 7, {}, {}, 0.0, 1.0, ASOF)
