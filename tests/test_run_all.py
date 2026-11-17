from datetime import date

import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.regime import Regime
from core.signals import Signal
from scanners.run_all import load_outputs, merge_ideas, merged_signal

ASOF = date(2026, 11, 16)


@pytest.fixture
def cfg():
    return load_settings(DEFAULT_SETTINGS)


def regime(label="strong", add=0.0, mult=1.0):
    return Regime(label, 6, 7, {}, {}, add, mult, ASOF)


def sig(ticker, skill, score, earnings=None, setup="x"):
    return Signal(ticker=ticker, company=ticker, skill=skill, score=score, confidence="med", thesis=f"{skill} thesis. More.",
                  signals_hit=[f"{skill}_hit"], entry_zone={"low": 100, "high": 101}, stop=95, targets=[120],
                  horizon_days=10, invalidation="x", key_dates=[{"date": earnings, "kind": "earnings"}] if earnings else [],
                  sources=[{"url": "https://example.com", "as_of": "2026-11-16"}],
                  metrics={"setup": setup, "price_as_of": "2026-11-16"})


def ctx(**over):
    c = {"earnings": {}, "estimates_falling": set(), "insider_sells": None}
    c.update(over)
    return c


def test_confluence_bonus_and_primary(cfg):
    sigs = {"breakout-scanner": [sig("AAA", "breakout-scanner", 60)], "political-edge": [sig("AAA", "political-edge", 70)],
            "catalyst-plays": [sig("BBB", "catalyst-plays", 50)]}
    ideas = merge_ideas(sigs, ctx(), regime(), cfg, ASOF)
    a = next(i for i in ideas if i.ticker == "AAA")
    assert a.primary.skill == "political-edge" and a.score == 80 and set(a.skills) == {"political-edge", "breakout-scanner"}
    m = merged_signal(a)
    assert m.score == 80 and "Also breakout-scanner: breakout-scanner thesis." in m.thesis
    assert set(m.signals_hit) == {"political-edge_hit", "breakout-scanner_hit"} and m.metrics["skills"]
    assert ideas[0].ticker == "AAA"


def test_regime_raises_threshold(cfg):
    sigs = {"catalyst-plays": [sig("BBB", "catalyst-plays", 50)]}
    assert merge_ideas(sigs, ctx(), regime(), cfg, ASOF)[0].rejected is None
    weak = merge_ideas(sigs, ctx(), regime("weak", 15, 0.5), cfg, ASOF)[0]
    assert weak.rejected and "< 55" in weak.rejected


def test_conflicts(cfg):
    sigs = {"breakout-scanner": [sig("AAA", "breakout-scanner", 80)],
            "momentum-hype": [sig("HYP", "momentum-hype", 80)],
            "political-edge": [sig("POL", "political-edge", 80)]}
    c = ctx(earnings={"AAA": date(2026, 11, 18)}, estimates_falling={"POL"}, insider_sells={"HYP": 3})
    ideas = {i.ticker: i for i in merge_ideas(sigs, c, regime(), cfg, ASOF)}
    assert any("earnings 2026-11-18 in 2 sessions" in x for x in ideas["AAA"].conflicts) and ideas["AAA"].score == 70
    assert any("insider selling" in x for x in ideas["HYP"].conflicts)
    assert any("estimates falling" in x for x in ideas["POL"].conflicts)
    m = merged_signal(ideas["AAA"])
    assert m.risk_flags[0].startswith("CONFLICT:")


def test_earnings_far_away_or_catalyst_skill_is_not_a_conflict(cfg):
    sigs = {"breakout-scanner": [sig("AAA", "breakout-scanner", 80, earnings=date(2026, 12, 10))],
            "catalyst-plays": [sig("CCC", "catalyst-plays", 80, earnings=date(2026, 11, 17))]}
    ideas = {i.ticker: i for i in merge_ideas(sigs, ctx(), regime(), cfg, ASOF)}
    assert ideas["AAA"].conflicts == [] and ideas["CCC"].conflicts == []


def test_insider_check_unavailable_is_a_note_not_a_conflict(cfg):
    ideas = merge_ideas({"momentum-hype": [sig("HYP", "momentum-hype", 80)]}, ctx(), regime(), cfg, ASOF)
    assert ideas[0].conflicts == [] and any("not checked" in n for n in ideas[0].notes)


def test_load_outputs_ignores_stale_files(cfg, tmp_path):
    import json
    import os
    import time
    cfg.raw["paths"]["reports_dir"] = str(tmp_path)
    p = tmp_path / f"breakout-{ASOF.isoformat()}.json"
    p.write_text(json.dumps([sig("AAA", "breakout-scanner", 60).model_dump(mode="json")]), encoding="utf-8")
    old = time.time() - 3600
    os.utime(p, (old, old))
    sigs, status = load_outputs(["breakout-scanner", "momentum-hype"], cfg, ASOF, since=time.time() - 60)
    assert sigs["breakout-scanner"] == [] and "predates" in status["breakout-scanner"]
    assert status["momentum-hype"] == "no output file"
    sigs, status = load_outputs(["breakout-scanner"], cfg, ASOF, since=None)
    assert len(sigs["breakout-scanner"]) == 1 and status["breakout-scanner"] == "ok"
