import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.review import bootstrap_ci, skill_table, suggestions, verdict


@pytest.fixture
def RV():
    return load_settings(DEFAULT_SETTINGS).section("review")


def closed(skill, rs, taken=1, signals='["vcp"]', exit_reason="stop"):
    return pd.DataFrame({"id": range(len(rs)), "skill": skill, "setup": "s", "status": "closed", "taken": taken,
                         "r_multiple": rs, "return_pct": [r * 0.05 for r in rs], "spy_return": 0.0,
                         "exit_reason": exit_reason, "signals_hit": signals})


def test_bootstrap_ci_brackets_mean():
    r = pd.Series([1.0, -1.0] * 50)
    lo, hi = bootstrap_ci(r, 2000)
    assert lo < 0 < hi and hi - lo < 0.5
    assert all(np.isnan(x) for x in bootstrap_ci(pd.Series(dtype=float), 100))


def test_verdicts(RV):
    assert verdict(10, (0.1, 0.5), RV).startswith("too early")
    assert verdict(40, (0.05, 0.4), RV) == "adding value"
    assert verdict(40, (-0.5, -0.1), RV).startswith("negative")
    assert verdict(40, (-0.2, 0.3), RV).startswith("inconclusive")


def test_skill_table_and_suggestions(RV):
    good = closed("breakout-scanner", [1.5, -1.0, 2.0, 1.0, -1.0, 1.2] * 6)       # 36 trades, positive
    bad = closed("momentum-hype", [-1.0, -1.0, 0.5, -1.0] * 9, signals='["bad_sig"]')   # 36 trades, negative
    allc = pd.concat([good, bad], ignore_index=True)
    sk = skill_table(allc, RV)
    assert sk.loc["breakout-scanner", "verdict"] == "adding value"
    assert sk.loc["momentum-hype", "verdict"].startswith("negative")
    by_signal = pd.DataFrame({"trades": [36], "expectancy_r": [-0.625]}, index=["bad_sig"])
    s = suggestions(allc, sk, by_signal, RV)
    assert any("momentum-hype" in x and "hype.min_score" in x for x in s)
    assert any("signal 'bad_sig'" in x for x in s)
    assert not any("breakout-scanner:" in x for x in s)


def test_filter_suggestion_both_directions(RV):
    taken = closed("x", [-1.0] * 20, taken=1)
    rejected = closed("x", [1.0] * 20, taken=0)
    s = suggestions(pd.concat([taken, rejected], ignore_index=True), pd.DataFrame(), pd.DataFrame(), RV)
    assert any("too strict" in x for x in s)
    s = suggestions(pd.concat([closed("x", [1.0] * 20, taken=1), closed("x", [-1.0] * 20, taken=0)], ignore_index=True),
                    pd.DataFrame(), pd.DataFrame(), RV)
    assert any("helping" in x for x in s)


def test_time_stop_suggestion(RV):
    t = closed("x", [0.4] * 40, exit_reason="time")
    assert any("time stop" in x for x in suggestions(t, pd.DataFrame(), pd.DataFrame(), RV))
