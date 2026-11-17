from datetime import date
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


EVENING_OF_SIGNAL = "2026-01-02T22:00:00+00:00"     # 5 pm ET on the signal date: earliest fill = next session


def backdate(db, iso=EVENING_OF_SIGNAL):
    """Fixtures use January bars; pretend the ideas were logged that evening, as a real run would."""
    import sqlite3
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE paper_trades SET logged_at = ?", (iso,))
    con.close()


def row_for(db, ticker):
    backdate(db)
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
    backdate(db)
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



def test_earliest_fill_index_rules():
    idx = pd.bdate_range("2026-11-12", periods=6)          # Thu 12 .. Thu 19
    assert journal.earliest_fill_index(idx, "2026-11-17T01:30:00+00:00") == 3   # Mon 16 20:30 ET -> Tue 17 (index 3)
    assert idx[journal.earliest_fill_index(idx, "2026-11-17T01:30:00+00:00")].date() == date(2026, 11, 17)
    assert idx[journal.earliest_fill_index(idx, "2026-11-16T13:00:00+00:00")].date() == date(2026, 11, 16)  # 8 am ET
    assert idx[journal.earliest_fill_index(idx, "2026-11-16T15:00:00+00:00")].date() == date(2026, 11, 17)  # 10 am ET
    assert journal.earliest_fill_index(idx, None) == 0


def test_no_fill_on_a_session_that_happened_before_the_idea_was_logged(db):
    """The 2026-11-16 case: an idea built on Friday's bar but logged Monday evening must not fill at Monday's open."""
    journal.log_signals(db, [sig(as_of="2026-01-02")], {"AAA": approve("AAA")})
    backdate(db, "2026-01-06T01:00:00+00:00")             # Mon Jan 5, 8 pm ET
    row = journal.read(db).iloc[0].to_dict()
    # Fri 2 signal; Mon 5 opens 101 (would have filled); Tue 6 opens 106 (above the 105 zone -> no fill)
    df = bars([(99, 100, 98, 100), (101, 103, 100, 102), (106, 108, 105, 107)])
    out = journal.evaluate_row(row, df, None, 5, 0.0, "stop_first")
    assert out["fill_date"] is None and out["status"] == "expired"          # next-open idea: one real chance, missed
    # same idea, Tuesday opens inside the zone -> fills Tuesday, never Monday
    df = bars([(99, 100, 98, 100), (101, 103, 100, 102), (104, 105, 103, 104.5)])
    out = journal.evaluate_row(row, df, None, 5, 0.0, "stop_first")
    assert out["fill_date"] == "2026-01-06" and out["fill_price"] == 104
    # Tuesday's bar not published yet -> still pending
    assert journal.evaluate_row(row, df.iloc[:2], None, 5, 0.0, "stop_first")["status"] == "pending"


def test_buy_stop_window_starts_after_logging_and_base_undercut_cancels(db):
    p = sig(setup="pre_breakout", entry_mode="buy_stop", cancel=90, stop=97)
    journal.log_signals(db, [p], {"AAA": approve("AAA")})
    backdate(db, "2026-01-06T01:00:00+00:00")             # logged Mon Jan 5 evening
    row = journal.read(db).iloc[0].to_dict()
    # Monday traded through the 100 trigger (before the idea existed): must NOT count as a fill
    df = bars([(96, 97, 95, 96), (99, 101, 98, 99.5), (99, 100.5, 98.5, 100)])
    out = journal.evaluate_row(row, df, None, 5, 0.0, "stop_first")
    assert out["fill_date"] == "2026-01-06" and out["fill_price"] == 100
    # base undercut on Monday (before the order existed) cancels it
    df = bars([(96, 97, 95, 96), (95, 96, 89, 92), (99, 101, 98, 100)])
    assert journal.evaluate_row(row, df, None, 5, 0.0, "stop_first")["fill_date"] is None
