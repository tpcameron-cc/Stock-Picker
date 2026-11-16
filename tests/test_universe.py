from datetime import date

import pandas as pd

from core.providers.nasdaqtrader import parse_directory
from core.universe import apply_filters, attach_risk_flags, reverse_split_flag, select_candidates
from tests.conftest import make_ohlcv

NASDAQ_TXT = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
ABCDW|ABCD Corp Warrant|S|N|N|100|N|N
File Creation Time: 1116202600:00|||||||
"""
OTHER_TXT = """ACT Symbol|Security Name|Exchange|CIK Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
JPM|JPMorgan Chase & Co. Common Stock|N|JPM|N|100|N|JPM
BRK.B|Berkshire Hathaway Class B|N|BRK.B|N|100|N|BRK.B
SPY|SPDR S&P 500|P|SPY|Y|100|N|SPY
File Creation Time: 1116202600:00|||||||
"""


def directory():
    return parse_directory(NASDAQ_TXT, OTHER_TXT)


def test_parse_directory():
    d = directory().set_index("symbol")
    assert set(d.index) == {"AAPL", "QQQ", "ZXZZT", "ABCDW", "JPM", "BRK.B", "SPY"}
    assert d.loc["AAPL", "exchange"] == "Q"
    assert d.loc["JPM", "exchange"] == "N"
    assert bool(d.loc["SPY", "is_etf"])


def test_select_candidates_drops_etfs_tests_and_odd_symbols(cfg):
    c = select_candidates(directory(), cfg)
    # BRK.B dropped by the '.' rule; ABCDW dropped by the name regex ("Warrant")
    assert set(c["symbol"]) == {"AAPL", "JPM"}


def test_name_regex_keeps_adrs_and_common(cfg):
    d = pd.DataFrame({
        "symbol": ["TSM", "XYZR", "XYZU", "XYZP", "ARM"],
        "name": ["Taiwan Semiconductor Manufacturing Company Ltd. American Depositary Shares",
                 "XYZ Acquisition Corp - Right", "XYZ Acquisition Corp - Units",
                 "XYZ Bank 6.5% Series A Preferred Stock", "Arm Holdings plc American Depositary Shares"],
        "exchange": "Q", "is_etf": False, "is_test": False,
    })
    assert set(select_candidates(d, cfg)["symbol"]) == {"TSM", "ARM"}


def test_select_candidates_list_marks_unknown_as_not_in_directory(cfg):
    c = select_candidates(directory(), cfg, only=["aapl", "PINKY"]).set_index("symbol")
    assert bool(c.loc["AAPL", "in_directory"]) and not bool(c.loc["PINKY", "in_directory"])


def _filter(cfg, close, volume, exchange="Q", in_dir=True, bars=100):
    cands = pd.DataFrame([{"symbol": "T", "name": "T", "exchange": exchange,
                           "is_etf": False, "is_test": False, "in_directory": in_dir}])
    hist = {} if close is None else {"T": make_ohlcv([close] * bars, volume=volume)}
    return apply_filters(cands, hist, cfg).iloc[0]


def test_liquid_stock_passes(cfg):
    r = _filter(cfg, 50.0, 1_000_000)            # $50M/day
    assert r["passed"] and r["reject_reasons"] == []
    assert r["avg_dollar_volume"] == 50_000_000


def test_price_floor(cfg):
    r = _filter(cfg, 2.99, 10_000_000)
    assert not r["passed"] and any("price" in x for x in r["reject_reasons"])


def test_price_exactly_at_floor_passes(cfg):
    assert _filter(cfg, 3.0, 10_000_000)["passed"]


def test_dollar_volume_floor(cfg):
    r = _filter(cfg, 10.0, 999_999)              # $9.99M < $10M
    assert not r["passed"] and any("avg $vol" in x for x in r["reject_reasons"])


def test_otc_and_bad_exchange_rejected(cfg):
    assert any("possible OTC" in x for x in _filter(cfg, 50.0, 1e6, in_dir=False)["reject_reasons"])
    assert any("exchange" in x for x in _filter(cfg, 50.0, 1e6, exchange="X")["reject_reasons"])


def test_missing_and_short_history(cfg):
    assert "no price data" in _filter(cfg, None, 0)["reject_reasons"]
    r = _filter(cfg, 50.0, 1e6, bars=30)
    assert any("bars" in x for x in r["reject_reasons"])


def test_thresholds_come_from_config(cfg):
    cfg.raw["filters"]["min_price"] = 100.0
    assert not _filter(cfg, 50.0, 1e6)["passed"]


def test_reverse_split_flag():
    today = date(2026, 11, 16)
    splits = pd.Series([2.0, 0.1], index=pd.to_datetime(["2024-01-01", "2026-06-01"]))
    assert reverse_split_flag(splits, 365, today) == "reverse split 1-for-10 on 2026-06-01"
    assert reverse_split_flag(splits, 100, today) is None          # outside window
    assert reverse_split_flag(pd.Series([2.0], index=pd.to_datetime(["2026-06-01"])), 365, today) is None
    assert reverse_split_flag(pd.Series(dtype=float), 365, today) is None


class FakePrices:
    def __init__(self, splits):
        self._s = splits

    def splits(self, t):
        if self._s is None:
            raise ConnectionError("down")
        return self._s


class FakeEdgar:
    def dilutive_filings(self, t, forms, lookback, today):
        return pd.DataFrame([{"form": "424B5", "filing_date": date(2026, 10, 1), "url": "https://sec.gov/x"}])


def _table():
    return pd.DataFrame([{"symbol": "T", "passed": True, "reject_reasons": []},
                         {"symbol": "U", "passed": False, "reject_reasons": ["no price data"]}])


def test_risk_flags_flag_but_do_not_reject_by_default(cfg):
    splits = pd.Series([0.2], index=pd.to_datetime(["2026-09-01"]))
    out = attach_risk_flags(_table(), FakePrices(splits), FakeEdgar(), cfg, today=date(2026, 11, 16))
    flags = out.iloc[0]["risk_flags"]
    assert any("reverse split 1-for-5" in f for f in flags) and any("424B5" in f for f in flags)
    assert out.iloc[0]["passed"]
    assert out.iloc[1]["risk_flags"] == []        # rejected rows aren't checked


def test_reject_on_flag_ignores_data_gaps(cfg):
    cfg.raw["risk_flags"]["reject_on_flag"] = True
    gap = attach_risk_flags(_table(), FakePrices(None), None, cfg).iloc[0]
    assert gap["passed"] and "unavailable" in gap["risk_flags"][0]
    hit = attach_risk_flags(_table(), FakePrices(pd.Series(dtype=float)), FakeEdgar(), cfg,
                            today=date(2026, 11, 16)).iloc[0]
    assert not hit["passed"] and "424B5" in hit["reject_reasons"][0]


def test_drop_incomplete_bar():
    from datetime import datetime, timezone

    from core.providers.yfinance_provider import drop_incomplete_bar

    df = make_ohlcv([10.0, 11.0], start="2026-11-13")          # Fri 13th, Mon 16th
    intraday = datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc)   # 12:00 ET Monday
    after_close = datetime(2026, 11, 16, 21, 30, tzinfo=timezone.utc)  # 16:30 ET
    assert len(drop_incomplete_bar(df, intraday)) == 1
    assert len(drop_incomplete_bar(df, after_close)) == 2
    next_day = datetime(2026, 11, 17, 14, 0, tzinfo=timezone.utc)
    assert len(drop_incomplete_bar(df, next_day)) == 2           # Monday bar is final by Tuesday
