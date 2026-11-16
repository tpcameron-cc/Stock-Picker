import json

import numpy as np
import pandas as pd
import pytest

from core import journal
from core.risk import SizingDecision
from core.signals import Signal


def sig(ticker="AAA", setup="breakout", entry_mode="next_open", cancel=None, low=100, high=105, stop=95,
        target=115, as_of="2026-01-02", hits=("vcp", "ma_stack")):
    return Signal(ticker=ticker, company=ticker, skill="breakout-scanner", score=70, confidence="med", thesis="t",
                  signals_hit=list(hits), entry_zone={"low": low, "high": high}, stop=stop, targets=[target],
                  horizon_days=5, invalidation="x",
                  sources=[{"url": "https://example.com", "as_of": as_of}],
                  metrics={"setup": setup, "price_as_of": as_of, "entry_mode": entry_mode, "cancel_below": cancel})


def bars(rows, start="2026-01-02"):
    o, h, l, c = zip(*rows)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1e6},
                        index=pd.bdate_range(start, periods=len(rows)))


def approve(t, shares=100):
    return SizingDecision(ticker=t, skill="breakout-scanner", score=70, approved=True, reward_risk=2.0,
                          shares=shares, risk_usd=600.0)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "j.sqlite"


def test_log_dedup_and_taken_flag(db):
    res = journal.log_signals(db, [sig("AAA"), sig("BBB")], {"AAA": approve("AAA")})
    assert res["inserted"] == 2
    again = journal.log_signals(db, [sig("AAA")], {"AAA": approve("AAA")})
    assert again == {"inserted": 0, "skipped_duplicates": 1, "run_id": again["run_id"]}
    t = journal.read(db).set_index("ticker")
    assert t.loc["AAA", "taken"] == 1 and t.loc["AAA", "shares"] == 100
    assert t.loc["BBB", "taken"] == 0 and t.loc["BBB", "sizing_notes"] == "not sized"
    assert json.loads(t.loc["AAA", "signals_hit"]) == ["vcp", "ma_stack"]
    assert list(journal.open_positions(db)["ticker"]) == ["AAA"]


def test_log_rejected_can_be_skipped(db):
    journal.log_signals(db, [sig("AAA"), sig("BBB")], {"AAA": approve("AAA")}, log_rejected=False)
    assert list(journal.read(db)["ticker"]) == ["AAA"]


def row_for(db, ticker):
    return journal.read(db, "ticker = ?", (ticker,)).iloc[0].to_dict()


def test_evaluate_target_hit_with_spy(db):
    journal.log_signals(db, [sig()], {"AAA": approve("AAA")})
    df = bars([(99, 100, 98, 100), (101, 103, 100, 102), (103, 116, 102, 114)])
    spy = pd.Series([400.0, 404.0, 408.0], index=df.index)
    out = journal.evaluate_row(row_for(db, "AAA"), df, spy, 5, 0.0, "stop_first")
    assert out["status"] == "closed" and out["exit_reason"] == "target"
    assert out["fill_price"] == 101 and out["exit_price"] == 115
    assert out["r_multiple"] == pytest.approx((115 - 101) / (101 - 95))
    assert out["spy_return"] == pytest.approx(408 / 400 - 1)        # day before fill -> exit day


def test_evaluate_open_then_time_stop(db):
    journal.log_signals(db, [sig()], {"AAA": approve("AAA")})
    row = row_for(db, "AAA")
    flat = [(99, 100, 98, 100)] + [(101, 102, 100, 101)] * 3
    out = journal.evaluate_row(row, bars(flat), None, 5, 0.0, "stop_first")
    assert out["status"] == "open" and out["exit_reason"] == "open" and out["exit_date"] is None
    out = journal.evaluate_row(row, bars(flat + [(101, 102, 100, 103)] * 2), None, 5, 0.0, "stop_first")
    assert out["status"] == "closed" and out["exit_reason"] == "time"     # 5 bars held


def test_evaluate_pending_then_expired(db):
    p = sig(setup="pre_breakout", entry_mode="buy_stop", cancel=90, stop=97)
    journal.log_signals(db, [p], {"AAA": approve("AAA")})
    row = row_for(db, "AAA")
    quiet = [(96, 97, 95, 96)]
    assert journal.evaluate_row(row, bars(quiet * 3), None, 5, 0.0, "stop_first")["status"] == "pending"
    assert journal.evaluate_row(row, bars(quiet * 6), None, 5, 0.0, "stop_first")["status"] == "expired"


def test_signal_date_missing_from_history(db):
    journal.log_signals(db, [sig(as_of="2025-12-01")], {"AAA": approve("AAA")})
    out = journal.evaluate_row(row_for(db, "AAA"), bars([(99, 100, 98, 100)]), None, 5, 0.0, "stop_first")
    assert "not in price history" in out["note"]


class FakeProvider:
    def __init__(self, hist):
        self.hist = hist

    def history(self, tickers, period="1y"):
        return {t: self.hist[t] for t in tickers if t in self.hist}


def test_update_outcomes_writes_db(db, cfg):
    journal.log_signals(db, [sig("AAA"), sig("BBB", stop=99)], {"AAA": approve("AAA")})
    df = bars([(99, 100, 98, 100), (101, 103, 100, 102), (103, 116, 102, 114)])
    loser = bars([(99, 100, 98, 100), (100.5, 101, 98, 98.5)])
    spy = bars([(400, 401, 399, 400), (400, 405, 399, 404), (404, 409, 403, 408)])
    upd = journal.update_outcomes(db, FakeProvider({"AAA": df, "BBB": loser, "SPY": spy}), cfg)
    assert len(upd) == 2
    t = journal.read(db).set_index("ticker")
    assert t.loc["AAA", "status"] == "closed" and t.loc["AAA", "exit_reason"] == "target"
    assert t.loc["BBB", "status"] == "closed" and t.loc["BBB", "exit_reason"] == "stop"
    assert journal.update_outcomes(db, FakeProvider({}), cfg).empty     # final trades aren't re-evaluated


def test_performance_math():
    t = pd.DataFrame({
        "id": range(5), "skill": ["s1", "s1", "s1", "s2", "s2"], "setup": ["a", "a", "b", "c", "c"],
        "status": ["closed"] * 4 + ["open"], "taken": [1, 1, 1, 1, 1],
        "return_pct": [0.10, -0.05, 0.02, -0.04, 0.5], "r_multiple": [2.0, -1.0, 0.4, -1.0, 9.0],
        "spy_return": [0.01, 0.01, -0.02, 0.0, 0.0], "exit_reason": ["target", "stop", "time", "stop", "open"],
        "signals_hit": ['["vcp"]', '["vcp", "squeeze"]', '["squeeze"]', "[]", '["vcp"]'],
    })
    p = journal.performance(t)
    o = p["overall"].loc["ALL"]
    assert o["trades"] == 4                                      # open trade excluded
    assert o["win_rate"] == 0.5
    assert o["expectancy_pct"] == pytest.approx((0.10 - 0.05 + 0.02 - 0.04) / 4)
    assert o["expectancy_r"] == pytest.approx(0.1)
    assert o["excess_vs_spy"] == pytest.approx(np.mean([0.09, -0.06, 0.04, -0.04]))
    assert p["by_skill"].loc["s1", "trades"] == 3
    assert p["by_signal"].loc["vcp", "trades"] == 2 and p["by_signal"].loc["squeeze", "trades"] == 2
    assert p["by_signal"].loc["vcp", "expectancy_r"] == pytest.approx(0.5)
    assert p["by_setup"].loc["s1/a", "trades"] == 2


def test_performance_taken_only():
    t = pd.DataFrame({"id": [1, 2], "skill": ["s", "s"], "setup": ["a", "a"], "status": ["closed", "closed"],
                      "taken": [1, 0], "return_pct": [0.1, -0.1], "r_multiple": [1.0, -1.0],
                      "spy_return": [0.0, 0.0], "exit_reason": ["target", "stop"], "signals_hit": ["[]", "[]"]})
    assert journal.performance(t)["overall"].loc["ALL", "trades"] == 1
    assert journal.performance(t, taken_only=False)["overall"].loc["ALL", "trades"] == 2
