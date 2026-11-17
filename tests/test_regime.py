import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.regime import evaluate_regime


@pytest.fixture
def R():
    return load_settings(DEFAULT_SETTINGS).section("regime")


IDX = pd.bdate_range("2025-01-01", periods=300)


def s(vals):
    return pd.Series(np.asarray(vals, float), index=IDX[: len(vals)])


def up(n=300, a=100, b=200):
    return s(np.linspace(a, b, n))


def down(n=300, a=200, b=100):
    return s(np.linspace(a, b, n))


def test_strong_regime(R):
    sectors = {f"S{i}": up() for i in range(11)}
    reg = evaluate_regime(up(), s([15.0] * 300), sectors, up(a=100, b=230), R)
    assert reg.label == "strong" and reg.points == 7 and reg.size_multiplier == 1.0 and reg.min_score_add == 0
    assert reg.detail["breadth"] == 1.0 and reg.detail["equal_weight_vs_spy"] > 0


def test_weak_regime_and_actions(R):
    sectors = {f"S{i}": down() for i in range(11)}
    vix = s([15.0] * 295 + [35.0] * 5)                   # spiking: 35 vs a 20-day average of 20
    reg = evaluate_regime(down(), vix, sectors, down(a=200, b=80), R)
    assert reg.label == "weak" and reg.points == 0
    assert reg.size_multiplier == 0.5 and reg.min_score_add == 15
    assert "WEAK" in reg.headline() and "cut to 50%" in reg.headline()


def test_neutral_and_missing_data(R):
    sectors = {f"S{i}": (up() if i < 6 else down()) for i in range(11)}     # 6/11 above -> breadth ok
    reg = evaluate_regime(up(), None, sectors, None, R)
    assert reg.checks["VIX calm"] is None and reg.checks["equal-weight keeping up"] is None
    assert reg.label == "neutral" and reg.points == 4
    assert "Unavailable" in reg.headline()


def test_vix_rising_check(R):
    sectors = {f"S{i}": up() for i in range(11)}
    vix = s([15.0] * 295 + [18.0] * 5)                   # below 20 but 18 / 15.75 = 1.14x its 20-day average
    reg = evaluate_regime(up(), vix, sectors, up(), R)
    assert reg.checks["VIX calm"] is True and reg.checks["VIX not rising"] is False
