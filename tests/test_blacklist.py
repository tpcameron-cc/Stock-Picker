from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.config import DEFAULT_SETTINGS, load_settings
from core.simulate import simulate_trade
from scanners.political.blacklist import build_blacklist, load_blacklist, reason, sale_fractions, save_blacklist

ASOF = date(2026, 11, 16)


@pytest.fixture
def B():
    return load_settings(DEFAULT_SETTINGS).section("political.blacklist")


def tx(rows):
    df = pd.DataFrame(rows, columns=["bioguide", "ticker", "type", "partial", "trade_date", "filing_date",
                                     "amount_low", "amount_high"])
    df["asset_type"] = "ST"
    df["source_url"] = "https://example.com/ptr.pdf"
    return df


def test_sale_fractions_estimate_from_disclosed_trades():
    t = tx([("M1", "AAA", "P", False, date(2026, 1, 5), date(2026, 1, 9), 15001, 50000),     # +32.5k
            ("M1", "AAA", "P", False, date(2026, 2, 5), date(2026, 2, 9), 15001, 50000),     # +32.5k -> 65k
            ("M1", "AAA", "S", True, date(2026, 9, 1), date(2026, 9, 5), 50001, 100000)])    # 75k partial
    f = sale_fractions(t)
    s = f[f["type"] == "S"].iloc[0]
    assert s["est_position_before"] == pytest.approx(65001)
    assert s["sale_fraction"] == 1.0                                   # 75k of ~65k: capped at 100%
    full = sale_fractions(tx([("M1", "BBB", "S", False, date(2026, 9, 1), date(2026, 9, 5), 1001, 15000)]))
    assert full.iloc[0]["sale_fraction"] == 1.0                        # reported full sale, no history needed
    unknown = sale_fractions(tx([("M1", "CCC", "S", True, date(2026, 9, 1), date(2026, 9, 5), 50001, 100000)]))
    assert unknown.iloc[0]["sale_fraction"] is None                    # partial, position bought before the data


def test_blacklist_rules(B):
    t = tx([
        ("Q1", "BIG", "S", False, date(2026, 10, 1), date(2026, 10, 10), 15001, 50000),   # full, >15k -> yes
        ("Q1", "SML", "S", False, date(2026, 10, 1), date(2026, 10, 10), 1001, 15000),    # full but <=15k -> no
        ("Q1", "PAR", "P", False, date(2026, 3, 1), date(2026, 3, 5), 50001, 100000),     # 75k position
        ("Q1", "PAR", "S", True, date(2026, 10, 2), date(2026, 10, 9), 15001, 50000),     # 32.5k = 43% -> no
        ("Q1", "MOST", "P", False, date(2026, 3, 1), date(2026, 3, 5), 15001, 50000),     # 32.5k position
        ("Q1", "MOST", "S", True, date(2026, 10, 2), date(2026, 10, 9), 15001, 50000),    # ~100% -> yes
        ("Q1", "OLD", "S", False, date(2026, 6, 1), date(2026, 6, 20), 50001, 100000),    # >90 days ago -> no
        ("NQ", "NOTQ", "S", False, date(2026, 10, 1), date(2026, 10, 10), 50001, 100000),  # not qualifying -> no
        ("Q1", "UNK", "S", True, date(2026, 10, 3), date(2026, 10, 9), 100001, 250000),   # partial, unknown -> no
    ])
    bl = build_blacklist(t, {"Q1"}, ASOF, B, {"Q1": "Rep. Example"})
    assert set(bl["ticker"]) == {"BIG", "MOST"}
    big = bl.set_index("ticker").loc["BIG"]
    assert big["until"] == date(2026, 12, 30) and bool(big["full_sale"]) and big["member"] == "Rep. Example"


def test_save_load_and_expiry(B, tmp_path):
    t = tx([("Q1", "BIG", "S", False, date(2026, 10, 1), date(2026, 10, 10), 15001, 50000)])
    bl = build_blacklist(t, {"Q1"}, ASOF, B, {"Q1": "Rep. Example"})
    p = save_blacklist(bl, tmp_path / "bl.json", ASOF)
    active, asof = load_blacklist(p, ASOF)
    assert asof == "2026-11-16" and "BIG" in active
    assert "blacklisted until 2026-12-30" in reason(active["BIG"]) and "whole position" in reason(active["BIG"])
    assert load_blacklist(p, date(2026, 12, 30))[0] == {}               # expired on its end date
    assert load_blacklist(tmp_path / "missing.json", ASOF) == ({}, None)


def test_run_all_blocks_blacklisted_ideas():
    from core.regime import Regime
    from core.signals import Signal
    from scanners.run_all import merge_ideas
    cfg = load_settings(DEFAULT_SETTINGS)
    s = Signal(ticker="BIG", company="Big", skill="breakout-scanner", score=95, confidence="high", thesis="t",
               entry_zone={"low": 100, "high": 101}, stop=95, targets=[120], horizon_days=10, invalidation="x",
               sources=[{"url": "https://example.com", "as_of": "2026-11-16"}], metrics={"price_as_of": "2026-11-16"})
    entry = {"ticker": "BIG", "member": "Rep. Example", "full_sale": True, "sale_fraction": 1.0, "amount": "$15,001–$50,000",
             "trade_date": "2026-10-01", "filing_date": "2026-10-10", "until": "2026-12-30"}
    ctx = {"earnings": {}, "estimates_falling": set(), "insider_sells": None, "blacklist": {"BIG": entry}}
    idea = merge_ideas({"breakout-scanner": [s]}, ctx, Regime("strong", 7, 7, {}, {}, 0, 1, ASOF), cfg, ASOF)[0]
    assert idea.rejected.startswith("blacklisted until 2026-12-30")


# ---------------- limit entry (political-edge) ----------------

def arrays(rows):
    return tuple(np.array(x, dtype=float) for x in zip(*rows))


def plan():
    return SimpleNamespace(setup="political_purchase", entry_low=98, entry_high=100, stop=92, target=116,
                           cancel_below=None, entry_mode="limit")


def test_limit_entry_waits_for_the_zone():
    # signal bar 0; opens above the zone for 3 days, then dips into it on day 4
    o, h, l, c = arrays([(101, 102, 100, 101), (104, 105, 103, 104), (105, 106, 104, 105), (103, 104, 102.5, 103),
                         (102, 103, 99.5, 101), (101, 117, 100, 115)])
    fi, xi, fpx, xpx, why = simulate_trade(o, h, l, c, 0, plan(), 10, 126)
    assert fi == 4 and fpx == 100 and why == "target"
    # one-chance window would have missed it
    assert simulate_trade(o, h, l, c, 0, plan(), 1, 126) is None


def test_limit_entry_fills_at_open_inside_zone_and_cancels_below_stop():
    o, h, l, c = arrays([(101, 102, 100, 101), (99, 100, 98, 99.5)])
    fi, _, fpx, _, _ = simulate_trade(o, h, l, c, 0, plan(), 10, 126)
    assert fi == 1 and fpx == 99
    o, h, l, c = arrays([(101, 102, 100, 101), (91, 92, 90, 91)])
    assert simulate_trade(o, h, l, c, 0, plan(), 10, 126) is None


def test_journal_uses_the_ideas_own_entry_window():
    import json
    from core.journal import entry_window
    assert entry_window({"signal_json": json.dumps({"metrics": {"entry_window_days": 10}})}, 5) == 10
    assert entry_window({"signal_json": json.dumps({"metrics": {}})}, 5) == 5
    assert entry_window({"signal_json": None}, 5) == 5
