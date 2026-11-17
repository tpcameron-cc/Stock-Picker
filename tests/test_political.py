from datetime import date

import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.providers.congress import match_member, members_frame, norm
from core.providers.edgar import parse_form4_purchases
from core.providers.house_disclosures import parse_amount, parse_ptr_text
from core.providers.usaspending import core_name, recipient_matches
from scanners.political.oversight import Oversight, classify
from scanners.political.score import score_purchase
from scanners.political.screen import apply_criteria, copy_trade


@pytest.fixture
def P():
    return load_settings(DEFAULT_SETTINGS).section("political")


# ---------------- PTR parsing (text shaped like real e-filed reports) ----------------

HEADER = """Filing ID #20034916
Name: Hon. Robert J. Wittman
Status: Member
State/District: VA01
ID Owner Asset Transaction Date Notification Amount Cap.
Type Date Gains >
$200?
"""
FOOTER = """* For the complete list of asset type abbreviations, please visit https://fd.house.gov/reference/asset-type-codes.aspx.
I CERTIFY that the statements I have made on the attached Periodic Transaction Report are true
"""


def test_parse_simple_and_wrapped_lines():
    text = HEADER + """Crown Castle Inc. Common Stock S 06/30/2026 07/02/2026 $1,001 - $15,000
(CCI) [ST]
F      S     : New
S          O : Morgan Stanley
SP NVIDIA Corporation - Common P 10/01/2026 10/02/2026 $15,001 -
Stock (NVDA) [ST] $50,000
F      S     : New
D          : Purchased in a managed account
by the advisor.
JT Apple Inc. (AAPL) [ST] S (partial) 01/05/2026 01/09/2026 $50,001 - $100,000
""" + FOOTER
    p = parse_ptr_text(text, "x")
    assert p.parsed and p.member_name == "Hon. Robert J. Wittman" and p.state_district == "VA01"
    a, b, c = p.transactions
    assert (a["ticker"], a["asset_type"], a["type"], a["owner"]) == ("CCI", "ST", "S", "self")
    assert a["trade_date"] == date(2026, 6, 30) and (a["amount_low"], a["amount_high"]) == (1001, 15000)
    assert (b["ticker"], b["owner"], b["amount_high"]) == ("NVDA", "SP", 50000)
    assert "$50,000" not in b["asset"]                      # wrapped amount removed from the asset text
    assert c["partial"] and c["type"] == "S" and c["owner"] == "JT"
    assert any("managed account" in n and "by the advisor" in n for n in p.notes)   # wrapped description kept


def test_page_header_not_spliced_into_asset():
    text = HEADER + "U.S. Physical Therapy, Inc. Common P 07/16/2026 08/14/2026 $1,001 - $15,000\n$200?\nStock (USPH) [ST]\n" + FOOTER
    t = parse_ptr_text(text).transactions[0]
    assert "$200?" not in t["asset"] and t["ticker"] == "USPH"


def test_spouse_over_amount_on_next_line():
    text = HEADER + "SP U.S. Treasury Note due 2/28/2029 P 12/15/2025 12/16/2025 Spouse/DC Over\n[GS] $1,000,000\n" + FOOTER
    t = parse_ptr_text(text).transactions[0]
    assert (t["amount_low"], t["amount_high"], t["asset_type"], t["ticker"]) == (1000000, None, "GS", None)


def test_scanned_and_empty():
    assert not parse_ptr_text("").parsed
    p = parse_ptr_text(HEADER + "nothing here\n" + FOOTER)
    assert not p.parsed and "no transactions recognized" in p.notes[0]


def test_parse_amount():
    assert parse_amount("$1,001 - $15,000") == (1001, 15000)
    assert parse_amount("Over $50,000,000") == (50000000, None)
    assert parse_amount("nothing") == (None, None)


# ---------------- members ----------------

def legislators():
    return [
        {"id": {"bioguide": "W000804"}, "name": {"first": "Robert", "last": "Wittman"},
         "terms": [{"type": "rep", "state": "VA", "district": 1, "party": "Republican", "end": "2027-01-03"}]},
        {"id": {"bioguide": "S000001"}, "name": {"first": "Linda", "last": "Sánchez"},
         "terms": [{"type": "rep", "state": "CA", "district": 38, "party": "Democrat", "end": "2027-01-03"}]},
        {"id": {"bioguide": "W000797"}, "name": {"first": "Debbie", "last": "Wasserman Schultz"},
         "terms": [{"type": "rep", "state": "FL", "district": 25, "party": "Democrat", "end": "2027-01-03"}]},
    ]


def test_match_member():
    m = members_frame(legislators())
    assert match_member(m, "Wittman", "VA01") == "W000804"
    assert match_member(m, "Sanchez", "CA38") == "S000001"          # accent-insensitive
    assert match_member(m, "Schultz", "FL25") == "W000797"          # compound surname, unique seat
    assert match_member(m, "Nobody", "TX99") is None
    assert norm("O'Rourke-Smith") == "orourkesmith"


# ---------------- copy-trade performance ----------------

IDX = pd.bdate_range("2026-01-01", periods=120)


def series(vals):
    return pd.Series(np.asarray(vals, float), index=IDX[: len(vals)])


def trades(*rows):
    return pd.DataFrame(rows, columns=["ticker", "type", "trade_date", "filing_date", "amount_low", "amount_high"])


def test_copy_trade_buy_and_hold_vs_spy():
    px = {"AAA": series([10] * 10 + [20] * 110)}
    spy = series([100] * 10 + [110] * 110)
    t = trades(("AAA", "P", IDX[0].date(), IDX[20].date(), 1001, 15000))
    r = copy_trade(t, px, spy, IDX[0].date(), IDX[-1].date(), delayed=False)
    assert r["capital"] == pytest.approx(8000.5)
    assert r["ret"] == pytest.approx(1.0) and r["spy_ret"] == pytest.approx(0.10) and r["excess"] == pytest.approx(0.9)
    d = copy_trade(t, px, spy, IDX[0].date(), IDX[-1].date(), delayed=True)    # enters at filing date (price 20)
    assert d["ret"] == pytest.approx(0.0) and d["excess"] == pytest.approx(0.0)
    assert r["open_value"] == pytest.approx(16001) and r["top_weight"] == 1.0


def test_copy_trade_fifo_sell_and_unmatched_sell():
    px = {"AAA": series([10] * 30 + [15] * 90)}
    spy = series([100] * 120)
    t = trades(("AAA", "P", IDX[0].date(), IDX[1].date(), 10000, 10000),       # 1000 sh @10
               ("AAA", "S", IDX[40].date(), IDX[41].date(), 7500, 7500),        # sells 500 sh @15
               ("BBB", "S", IDX[5].date(), IDX[6].date(), 1000, 1000))          # never bought in window
    px["BBB"] = series([5] * 120)
    r = copy_trade(t, px, spy, IDX[0].date(), IDX[-1].date(), delayed=False)
    assert r["pnl"] == pytest.approx(2500 + 2500)                               # realized + open half
    assert r["open_value"] == pytest.approx(7500) and r["unmatched_sells"] == 1


def test_best_position_removed():
    px = {"WIN": series([10] * 5 + [40] * 115), "MEH": series([10] * 5 + [9] * 115)}
    spy = series([100] * 120)
    t = trades(("WIN", "P", IDX[0].date(), IDX[1].date(), 1000, 1000),
               ("MEH", "P", IDX[0].date(), IDX[1].date(), 1000, 1000))
    r = copy_trade(t, px, spy, IDX[0].date(), IDX[-1].date(), delayed=False)
    assert r["excess"] > 0 and r["best_ticker"] == "WIN" and r["excess_ex_best"] == pytest.approx(-0.1)


def stats(**over):
    s = {"excess_1y": 0.08, "reliability_gap_pp": 2.0, "n_trades": 25, "excess_ex_best": 0.03, "best_ticker": "NVDA",
         "median_filing_delay": 20.0, "late_share": 0.05, "est_portfolio": 2e6, "est_portfolio_low": 1.5e6,
         "est_portfolio_high": 3e6, "top_weight": 0.2, "managed_or_blind": False, "options_share": 0.0}
    s.update(over)
    return s


def test_criteria(P):
    S = P["screen"]
    assert apply_criteria(stats(), S)[0]
    for over, msg in [({"excess_1y": -0.02}, "trails SPY"), ({"reliability_gap_pp": 9.0}, "unreliable"),
                      ({"n_trades": 4}, "trades"), ({"excess_ex_best": -0.01}, "only because of NVDA"),
                      ({"median_filing_delay": 40.0}, "median filing delay"), ({"late_share": 0.5}, "chronic late"),
                      ({"est_portfolio": 2e5, "est_portfolio_high": 4e5, "excess_1y": 0.05}, "portfolio")]:
        ok, fails, *_ = apply_criteria(stats(**over), S)
        assert not ok and any(msg in f for f in fails), over
    ok, _, flags, exc = apply_criteria(stats(est_portfolio=9e5, est_portfolio_low=5e5, est_portfolio_high=1.4e6), S)
    assert ok and any("borderline" in f for f in flags) and exc is None
    ok, _, _, exc = apply_criteria(stats(est_portfolio=2e5, est_portfolio_high=3e5, excess_1y=0.15), S)
    assert ok and "beats SPY by" in exc
    ok, _, _, exc = apply_criteria(stats(est_portfolio=2e5, est_portfolio_high=3e5, top_weight=0.6), S)
    assert ok and "unusual allocation" in exc
    _, _, flags, _ = apply_criteria(stats(managed_or_blind=True, options_share=0.5), S)
    assert any("blind trust" in f for f in flags) and any("options-heavy" in f for f in flags)


# ---------------- classification, timing, score ----------------

def contracts(rows):
    return pd.DataFrame(rows, columns=["award_id", "recipient", "action_date", "amount", "agency", "sub_agency", "url"])


def test_classify_direct_sector_none_and_timing(P):
    PU = P["purchases"]
    ov = Oversight(agencies={"Department of Defense"}, industries={"Aerospace & Defense"})
    c = contracts([("1", "LOCKHEED MARTIN CORP", date(2026, 3, 10), 50e6, "Department of Defense", "Department of the Army", "u1"),
                   ("2", "LOCKHEED MARTIN CORP", date(2026, 1, 5), 20e6, "Department of Energy", "x", "u2")])
    r = classify(ov, "Industrials", "Aerospace & Defense", c, date(2026, 3, 1), PU)
    assert r["link"] == "direct" and r["related_contract_usd"] == 50e6
    assert len(r["timing_flags"]) == 1 and "traded 9d before $50M" in r["timing_flags"][0]
    r = classify(ov, "Industrials", "Aerospace & Defense", contracts([]), date(2026, 3, 1), PU)
    assert r["link"] == "sector" and "curated map" in r["evidence"][0]
    r = classify(ov, "Technology", "Software", contracts([]), date(2026, 3, 1), PU)
    assert r["link"] == "none"
    r = classify(ov, "Technology", "Software", None, date(2026, 3, 1), PU)
    assert "federal contract data unavailable" in r["gaps"]
    broad = Oversight(broad=True)
    r = classify(broad, "Technology", "Software", c, date(2026, 1, 1), PU)
    assert r["link"] == "direct" and r["related_contract_usd"] == 70e6


def test_score(P):
    sc, lb, md = P["scoring"], P["purchases"]["lookback_days"], P["screen"]["max_median_filing_delay"]
    best, parts = score_purchase("direct", 3, 250000, 10, 10, 0.05, True, sc, lb, md)
    assert parts["agreement"] == 20 and parts["size"] == pytest.approx(15) and parts["recency"] == 10
    assert best == pytest.approx(40 + 20 + 15 + 10 + 10)
    s, parts = score_purchase("sector", 0, 8000, 60, 45, 0.30, False, sc, lb, md)
    assert parts["late_filing"] == -10 and parts["price_move"] == pytest.approx(-15)
    assert parts["recency"] == pytest.approx(5)
    assert score_purchase("none", 0, 0, 100, 0, None, False, sc, lb, md)[0] == 0


# ---------------- Form 4, recipient names ----------------

FORM4 = """<?xml version="1.0"?><ownershipDocument>
<reportingOwner><reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId></reportingOwner>
<nonDerivativeTable>
<nonDerivativeTransaction><transactionDate><value>2026-10-01</value></transactionDate>
<transactionCoding><transactionCode>P</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>1000</value></transactionShares>
<transactionPricePerShare><value>25.5</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
<nonDerivativeTransaction><transactionDate><value>2026-10-02</value></transactionDate>
<transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>50</value></transactionShares>
<transactionPricePerShare><value>26</value></transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable></ownershipDocument>"""


def test_form4_purchases_only():
    rows = parse_form4_purchases(FORM4)
    assert rows == [{"owner": "DOE JANE", "trade_date": "2026-10-01", "shares": 1000.0, "price": 25.5, "value": 25500.0}]
    assert parse_form4_purchases("<not xml") == []


def test_recipient_name_matching():
    assert core_name("Lockheed Martin Corporation - Common Stock") == "lockheed martin"
    assert core_name("Apple Inc. - Common Stock") == "apple"
    assert recipient_matches("LOCKHEED MARTIN CORP", "lockheed martin")
    assert not recipient_matches("RAYTHEON/LOCKHEED MARTIN JAVELIN JV", "lockheed martin")   # JV excluded
    assert not recipient_matches("APPLETON MANUFACTURING", "apple")


def test_account_captured_so_same_day_buys_in_two_accounts_are_distinct():
    text = HEADER + """AT&T Inc. (T) [ST] P 11/03/2026 11/12/2026 $1,001 - $15,000
F      S     : New
S          O : David Taylor Trust > Sardinia Ready Mix 401(k) - Dave
AT&T Inc. (T) [ST] P 11/03/2026 11/12/2026 $1,001 - $15,000
F      S     : New
S          O : David Taylor Trust > Schwab Joint Brokerage #1
(Home Grown)
""" + FOOTER
    a, b = parse_ptr_text(text).transactions
    assert a["account"].endswith("401(k) - Dave")
    assert b["account"] == "David Taylor Trust > Schwab Joint Brokerage #1 (Home Grown)"


def test_broad_vendor_flag_and_usd(P):
    from scanners.political.oversight import usd
    PU = P["purchases"]
    ov = Oversight(agencies={"Department of Transportation"})
    c = contracts([("1", "AT&T CORP", date(2026, 3, 10), 163e6, "Department of Transportation", "FAA", "u"),
                   ("2", "AT&T CORP", date(2026, 3, 10), 900e6, "Department of Defense", "DISA", "u")])
    r = classify(ov, "Communication Services", "Telecom Services", c, date(2026, 1, 1), PU)
    assert r["link"] == "direct" and r["committee_contract_share"] == pytest.approx(163 / 1063)
    assert any("broad government vendor" in f for f in r["flags"]) and "15% of its $1.1B" in r["evidence"][0]
    assert usd(2.5e9) == "$2.5B" and usd(163e6) == "$163M" and usd(4e5) == "$0.4M"
