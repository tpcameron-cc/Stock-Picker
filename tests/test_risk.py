import numpy as np
import pandas as pd
import pytest
import yaml

from core.config import DEFAULT_SETTINGS, load_settings, set_local
from core.risk import max_correlation, position_size, size_signals
from core.signals import Signal


def sig(ticker, score=70, low=100, high=101, stop=95, target=120, skill="breakout-scanner"):
    return Signal(ticker=ticker, company=ticker, skill=skill, score=score, confidence="med", thesis="t",
                  entry_zone={"low": low, "high": high}, stop=stop, targets=[target], horizon_days=20,
                  invalidation="x", sources=[{"url": "https://example.com", "as_of": "2026-11-13"}])


def test_position_size_math():
    # $100k x 0.75% = $750 risk; entry 101 - stop 95 = $6/share -> 125 shares
    assert position_size(100_000, 0.0075, 101, 95, 0.20) == (125, False)
    # tight stop: 750 / 0.5 = 1500 shares = $151.5k > 20% cap ($20k / 101 = 198 shares)
    assert position_size(100_000, 0.0075, 101, 100.5, 0.20) == (198, True)
    # multiplier halves risk
    assert position_size(100_000, 0.0075, 101, 95, 0.20, multiplier=0.5) == (62, False)
    with pytest.raises(ValueError):
        position_size(100_000, 0.0075, 95, 101, 0.20)


def test_rr_rejection_and_sizing(cfg):
    good = sig("AAA", target=120)              # mid 100.5, risk 5.5, reward 19.5 -> 3.5
    poor = sig("BBB", target=106)              # reward 5.5 / 5.5 = 1.0
    d = {x.ticker: x for x in size_signals([good, poor], cfg, account=100_000)}
    assert d["AAA"].approved and d["AAA"].shares == 125 and d["AAA"].risk_usd == pytest.approx(750)
    assert d["AAA"].risk_pct_of_account == pytest.approx(0.0075)
    assert not d["BBB"].approved and "R:R" in d["BBB"].reasons[0]


def test_no_account_still_decides_but_no_shares(cfg):
    d = size_signals([sig("AAA")], cfg, account=None)[0]
    assert d.approved and d.shares is None and any("account size not set" in f for f in d.flags)


def test_max_open_positions_counts_existing_and_batch(cfg):
    cfg.raw["risk"]["max_open_positions"] = 3
    existing = [{"ticker": "X1", "sector": "Tech"}, {"ticker": "X2", "sector": "Energy"}]
    sigs = [sig("A", score=90), sig("B", score=80)]
    d = {x.ticker: x for x in size_signals(sigs, cfg, existing, account=100_000)}
    assert d["A"].approved                     # highest score fills the last slot
    assert not d["B"].approved and "max open positions" in d["B"].reasons[0]


def test_sector_cap_and_unknown_sector(cfg):
    sectors = {"A": "Tech", "B": "Tech", "C": "Tech", "D": None}
    existing = [{"ticker": "X1", "sector": "Tech"}]
    sigs = [sig("A", 90), sig("B", 80), sig("C", 70), sig("D", 60)]
    d = {x.ticker: x for x in size_signals(sigs, cfg, existing, sectors.get, account=100_000)}
    assert d["A"].approved and not d["B"].approved and "sector cap" in d["B"].reasons[0]
    assert not d["C"].approved
    assert d["D"].approved and any("sector unknown" in f for f in d["D"].flags)


def test_already_held_rejected(cfg):
    d = size_signals([sig("A")], cfg, [{"ticker": "A", "sector": None}], account=100_000)[0]
    assert not d.approved and "already in the book" in d.reasons[0]


def returns(seed, n=80, base=None, noise=1.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-06-01", periods=n)
    x = rng.normal(0, 0.01, n) if base is None else base.to_numpy() + rng.normal(0, 0.01 * noise, n)
    return pd.Series(x, index=idx)


def test_correlation_check(cfg):
    held = returns(1)
    twin = returns(2, base=held, noise=0.2)   # highly correlated
    other = returns(3)
    rets = {"H": held, "TWIN": twin, "OTHER": other}
    existing = [{"ticker": "H", "sector": None}]
    d = {x.ticker: x for x in size_signals([sig("TWIN", 90), sig("OTHER", 80)], cfg, existing,
                                           returns=rets, account=100_000)}
    assert not d["TWIN"].approved and "correlation" in d["TWIN"].reasons[0] and d["TWIN"].corr_with == "H"
    assert d["OTHER"].approved and d["OTHER"].max_corr < 0.5
    cfg.raw["risk"]["correlation_action"] = "flag"
    d = size_signals([sig("TWIN")], cfg, existing, returns=rets, account=100_000)[0]
    assert d.approved and any("correlation" in f for f in d.flags)


def test_correlation_insufficient_data_is_flagged(cfg):
    short = returns(1, n=10)
    d = size_signals([sig("A")], cfg, [{"ticker": "H", "sector": None}],
                     returns={"A": short, "H": returns(2)}, account=100_000)[0]
    assert d.approved and any("correlation unchecked" in f for f in d.flags)
    assert max_correlation(None, {"H": returns(2)}, 60) == (None, None)


def test_local_overlay(tmp_path):
    local = tmp_path / "local.yaml"
    set_local("account.size_usd", 50000.0, local)
    s = load_settings(DEFAULT_SETTINGS, local)
    assert s.get("account.size_usd") == 50000.0
    assert s.get("risk.risk_per_trade_pct") == 0.0075          # untouched keys still from settings.yaml
    assert yaml.safe_load(local.read_text(encoding="utf-8"))["account"]["size_usd"] == 50000.0
    assert load_settings(DEFAULT_SETTINGS).get("account.size_usd") is None   # explicit path: no overlay
