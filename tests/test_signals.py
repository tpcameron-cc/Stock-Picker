from datetime import date

import pytest
from pydantic import ValidationError

from core.signals import Confidence, Signal, load_signals, save_signals


def base(**over):
    d = dict(
        ticker="abc", company="ABC Corp", skill="breakout-scanner", score=72,
        confidence="med", thesis="Tight base near highs on rising volume.",
        signals_hit=["vcp", "ma_stack"], entry_zone={"low": 50, "high": 51}, stop=47,
        targets=[58, 62], horizon_days=20, invalidation="Close below 47.",
        sources=[{"url": "https://finance.yahoo.com/quote/ABC", "as_of": "2026-11-13"}],
    )
    d.update(over)
    return d


def test_valid_signal_and_derived_fields():
    s = Signal(**base())
    assert s.ticker == "ABC"
    assert s.confidence is Confidence.med
    assert s.entry_mid == 50.5
    assert s.reward_risk == pytest.approx((58 - 50.5) / (50.5 - 47))
    assert s.generated_at.tzinfo is not None


@pytest.mark.parametrize("over", [
    {"score": 101}, {"score": -1}, {"confidence": "very high"},
    {"sources": []}, {"targets": []}, {"thesis": ""},
    {"stop": 50.5},                              # stop inside entry zone
    {"targets": [50.5]},                         # target not above entry
    {"entry_zone": {"low": 52, "high": 51}},     # inverted zone
    {"sources": [{"url": "not a url", "as_of": "2026-11-13"}]},
    {"sources": [{"url": "https://x.com"}]},     # missing as_of
    {"horizon_days": 0},
])
def test_invalid_signals_rejected(over):
    with pytest.raises(ValidationError):
        Signal(**base(**over))


def test_round_trip_json_and_markdown(tmp_path):
    sigs = [
        Signal(**base()),
        Signal(**base(ticker="XYZ", score=90, key_dates=[{"date": "2026-12-01", "kind": "earnings"}])),
    ]
    jp, mp = save_signals(sigs, tmp_path, "test")
    back = load_signals(jp)
    assert [s.ticker for s in back] == ["ABC", "XYZ"]
    assert back[1].key_dates[0].date == date(2026, 12, 1)
    md = mp.read_text(encoding="utf-8")
    assert md.index("XYZ") < md.index("ABC")     # sorted by score
    assert "as of 2026-11-13" in md
    assert "2026-12-01 earnings" in md
