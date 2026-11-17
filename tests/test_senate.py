from datetime import date

import pandas as pd

from core.providers.congress import match_senator, members_frame
from core.providers.senate_disclosures import parse_ptr_html, parse_search_row
from scanners.political.screen import dedupe_cross_filing

# Shaped like a real eFD electronic PTR page (whitespace and links as served)
PAGE = """<table class="table table-striped"><thead><tr class="header"><th>#</th></tr></thead>
<tbody>
  <tr>
    <td>2</td>
    <td>
        10/21/2026
    </td>
    <td>Spouse</td>
    <td>
        <a href="https://finance.yahoo.com/quote/MSFT"
           target="_blank">MSFT</a>
    </td>
    <td>
        Microsoft Corporation - Common Stock
    </td>
    <td>Stock</td>
    <td>Sale (Partial)</td>
    <td>$1,001 - $15,000</td>
    <td>--</td>
  </tr>
  <tr>
    <td>1</td>
    <td>10/20/2026</td>
    <td>Self</td>
    <td>--</td>
    <td>US Treasury Bill 0% due 01/15/2027</td>
    <td>Government Security</td>
    <td>Purchase</td>
    <td>$250,001 - $500,000</td>
    <td>Bought at auction &amp; held</td>
  </tr>
  <tr>
    <td>3</td>
    <td>10/22/2026</td>
    <td>Joint</td>
    <td><a href="https://finance.yahoo.com/quote/BRK.B">BRK.B</a></td>
    <td>Berkshire Hathaway Inc. Class B</td>
    <td>Stock Option</td>
    <td>Purchase</td>
    <td>$15,001 - $50,000</td>
    <td>--</td>
  </tr>
</tbody></table>"""


def test_parse_senate_ptr_html():
    a, b, c = parse_ptr_html(PAGE)
    assert (a["ticker"], a["owner"], a["type"], a["partial"], a["asset_type"]) == ("MSFT", "SP", "S", True, "ST")
    assert a["trade_date"] == date(2026, 10, 21) and (a["amount_low"], a["amount_high"]) == (1001, 15000)
    assert a["comment"] == ""
    assert b["ticker"] is None and b["asset_type"] == "GS" and b["type"] == "P" and b["owner"] == "self"
    assert b["comment"] == "Bought at auction & held"
    assert c["ticker"] == "BRK-B" and c["asset_type"] == "OP" and c["owner"] == "JT"
    assert parse_ptr_html("<html>no table</html>") == []


def test_parse_search_row():
    r = parse_search_row(["Shelley M", "Capito", "Capito, Shelley Moore (Senator)",
                          '<a href="/search/view/ptr/a89c0c6b-4c12/" target="_blank">Periodic Transaction Report for 11/04/2026</a>',
                          "11/04/2026"])
    assert r["doc_id"] == "a89c0c6b-4c12" and r["is_senator"] and not r["paper"] and not r["amendment"]
    assert r["filing_date"] == date(2026, 11, 4)
    p = parse_search_row(["A", "B", "B, A (Candidate)",
                          '<a href="/search/view/paper/xyz/">Periodic Transaction Report (Amendment 1)</a>', "10/01/2026"])
    assert p["paper"] and p["amendment"] and not p["is_senator"]
    assert parse_search_row(["A", "B", "x", "no link", "10/01/2026"]) is None


def senators():
    def s(bg, first, last, state, nick=None):
        n = {"first": first, "last": last}
        if nick:
            n["nickname"] = nick
        return {"id": {"bioguide": bg}, "name": n,
                "terms": [{"type": "sen", "state": state, "party": "Democrat", "end": "2029-01-03"}]}
    return [s("F000479", "John", "Fetterman", "PA"), s("S001", "Rick", "Scott", "FL"),
            s("S002", "Tim", "Scott", "SC"), s("V001", "Chris", "Van Hollen", "MD"),
            s("C001", "Shelley", "Capito", "WV", nick="Shelley Moore")]


def test_match_senator():
    m = members_frame(senators())
    assert match_senator(m, "John K", "Fetterman") == "F000479"
    assert match_senator(m, "Tim", "Scott") == "S002" and match_senator(m, "Rick", "Scott") == "S001"
    assert match_senator(m, "Bob", "Scott") is None                # ambiguous: never guess
    assert match_senator(m, "Chris", "Van Hollen Jr") == "V001"    # compound surname
    assert match_senator(m, "Shelley M", "Capito") == "C001"


def test_dedupe_cross_filing_keeps_latest_and_same_filing_repeats():
    base = {"chamber": "senate", "last": "Doe", "asset": "Microsoft", "type": "P", "trade_date": date(2026, 9, 1),
            "owner": "self", "amount_low": 1001.0, "amount_high": 15000.0}
    tx = pd.DataFrame([
        {**base, "doc_id": "orig", "filing_date": date(2026, 9, 20)},
        {**base, "doc_id": "amend", "filing_date": date(2026, 10, 5)},                 # amendment repeats it
        {**base, "doc_id": "acct", "filing_date": date(2026, 9, 25), "trade_date": date(2026, 9, 3)},
        {**base, "doc_id": "acct", "filing_date": date(2026, 9, 25), "trade_date": date(2026, 9, 3)},   # 2 accounts
    ])
    out, removed = dedupe_cross_filing(tx)
    assert removed == 1
    assert sorted(out["doc_id"]) == ["acct", "acct", "amend"]
    assert dedupe_cross_filing(pd.DataFrame())[1] == 0


def test_dedupe_handles_missing_high_amount():
    row = {"chamber": "house", "last": "Matsui", "asset": "T-Note", "type": "P", "trade_date": date(2025, 12, 15),
           "owner": "SP", "amount_low": 1000000.0, "amount_high": None, "filing_date": date(2026, 1, 9)}
    out, removed = dedupe_cross_filing(pd.DataFrame([{**row, "doc_id": "a"}, {**row, "doc_id": "b"}]))
    assert removed == 1 and list(out["doc_id"]) == ["b"]
