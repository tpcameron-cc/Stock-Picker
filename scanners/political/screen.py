"""Step A: screen members of Congress by how their DISCLOSED trades performed.

Only Periodic Transaction Reports are available (no full holdings), so performance is a "copy-trade" result:
each disclosed stock purchase in the window buys the amount-range midpoint at the close on the trade date
(or, delay-adjusted, on the filing date), disclosed sales close it out FIFO (sales of positions bought before
the window are ignored), and what's left is marked to market. SPY gets exactly the same cash flows.
Return = total P&L / total dollars bought. Portfolio size = open positions at market value, i.e. the
PTR-TRADED book, not the member's full net worth.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import pandas as pd


DEDUPE_KEY = ["chamber", "last", "asset", "type", "trade_date", "owner", "amount_low", "amount_high"]


def dedupe_cross_filing(tx: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Amended reports repeat the original's transactions in a NEW filing. When an identical transaction appears in
    more than one filing, keep only the latest filing's copy. Repeats inside one filing are kept: those are real
    separate trades (e.g. the same stock bought in two accounts). Returns (deduped, rows_removed)."""
    if tx.empty:
        return tx, 0
    t = tx.copy()
    # map(str) per cell: astype(str) keeps NaN as a float under pandas' string dtype ("Over $X" has no high amount)
    t["_k"] = t[[c for c in DEDUPE_KEY if c in t]].apply(lambda col: col.map(str)).agg("|".join, axis=1)
    docs = t.groupby("_k")["doc_id"].nunique()
    multi = t["_k"].isin(docs[docs > 1].index)
    latest = t[multi].sort_values(["filing_date", "doc_id"]).groupby("_k")["doc_id"].last()
    keep = ~multi | (t["doc_id"] == t["_k"].map(latest))
    return t[keep].drop(columns="_k"), int((~keep).sum())


def amount_mid(lo: float | None, hi: float | None) -> float | None:
    if lo is None or pd.isna(lo):
        return None
    return (lo + hi) / 2 if hi is not None and not pd.isna(hi) else float(lo)


def price_on_or_after(close: pd.Series, d: date) -> float | None:
    i = close.index.searchsorted(pd.Timestamp(d), side="left")
    return float(close.iloc[i]) if i < len(close) else None


def price_on_or_before(close: pd.Series, d: date) -> float | None:
    s = close.loc[: pd.Timestamp(d)]
    return float(s.iloc[-1]) if len(s) else None


def copy_trade(trades: pd.DataFrame, prices: dict[str, pd.Series], spy: pd.Series, start: date, end: date,
               delayed: bool) -> dict:
    """Copy-trade P&L for one member. `trades`: ticker, type (P/S), trade_date, filing_date, amount_low, amount_high
    (stocks only). `prices`: ticker -> close series. Returns totals, per-ticker P&L and the open book."""
    lots: dict[str, list[dict]] = defaultdict(list)
    pnl, spy_pnl, cap = defaultdict(float), defaultdict(float), defaultdict(float)
    unpriced = unmatched_sells = 0

    def eff(row) -> date | None:
        return row.filing_date if delayed else row.trade_date

    t = trades.copy()
    t["eff"] = [eff(r) for r in t.itertuples()]
    t = t[t["eff"].notna()]
    t = t[(t["eff"] <= end)].sort_values(["eff", "type"])          # P before S on the same day
    for r in t.itertuples():
        close = prices.get(r.ticker)
        mid = amount_mid(r.amount_low, r.amount_high)
        if close is None or mid is None:
            unpriced += 1
            continue
        if r.type == "P":
            if not (start <= r.trade_date <= end):
                continue
            px, spx = price_on_or_after(close, r.eff), price_on_or_after(spy, r.eff)
            if px is None or spx is None or pd.Timestamp(r.eff) > close.index[-1]:
                unpriced += 1
                continue
            lots[r.ticker].append({"shares": mid / px, "cost": mid, "spy_units": mid / spx, "mid": mid,
                                   "lo": r.amount_low, "hi": r.amount_high})
            cap[r.ticker] += mid
        elif r.type == "S":
            if not lots.get(r.ticker):
                unmatched_sells += 1
                continue
            px, spx = price_on_or_after(close, r.eff), price_on_or_after(spy, r.eff)
            if px is None or spx is None:
                continue
            to_sell = mid / px
            for lot in lots[r.ticker]:
                if to_sell <= 0:
                    break
                if lot["shares"] <= 0:
                    continue
                sh = min(lot["shares"], to_sell)
                frac = sh / lot["shares"]
                pnl[r.ticker] += sh * px - lot["cost"] * frac
                spy_pnl[r.ticker] += lot["spy_units"] * frac * spx - lot["cost"] * frac
                for k in ("shares", "cost", "spy_units", "mid", "lo", "hi"):
                    if lot[k] is not None and not pd.isna(lot[k]):
                        lot[k] *= (1 - frac)
                to_sell -= sh
            lots[r.ticker] = [l for l in lots[r.ticker] if l["shares"] > 1e-9]

    spy_end = price_on_or_before(spy, end)
    open_val, open_lo, open_hi = defaultdict(float), 0.0, 0.0
    for tk, ls in lots.items():
        px = price_on_or_before(prices[tk], end)
        for lot in ls:
            v = lot["shares"] * px
            open_val[tk] += v
            pnl[tk] += v - lot["cost"]
            spy_pnl[tk] += lot["spy_units"] * spy_end - lot["cost"]
            scale = v / lot["mid"] if lot["mid"] else 0
            open_lo += (lot["lo"] or 0) * scale
            open_hi += (lot["hi"] if lot["hi"] and not pd.isna(lot["hi"]) else lot["lo"] or 0) * scale

    capital = sum(cap.values())
    total, spy_total = sum(pnl.values()), sum(spy_pnl.values())
    out = {"capital": capital, "pnl": total, "spy_pnl": spy_total,
           "ret": total / capital if capital else np.nan, "spy_ret": spy_total / capital if capital else np.nan,
           "open_value": sum(open_val.values()), "open_value_low": open_lo, "open_value_high": open_hi,
           "pnl_by_ticker": dict(pnl), "spy_pnl_by_ticker": dict(spy_pnl), "capital_by_ticker": dict(cap),
           "open_by_ticker": dict(open_val), "unpriced": unpriced, "unmatched_sells": unmatched_sells}
    out["excess"] = out["ret"] - out["spy_ret"] if capital else np.nan
    if pnl:
        best = max(pnl, key=lambda k: pnl[k] - spy_pnl.get(k, 0))
        c_ex = capital - cap.get(best, 0)
        out["best_ticker"] = best
        out["excess_ex_best"] = ((total - pnl[best]) - (spy_total - spy_pnl.get(best, 0))) / c_ex if c_ex > 0 else np.nan
    else:
        out["best_ticker"], out["excess_ex_best"] = None, np.nan
    ov = out["open_value"]
    out["top_weight"] = max(open_val.values()) / ov if ov > 0 else np.nan
    return out


def member_stats(tx: pd.DataFrame, prices: dict[str, pd.Series], spy: pd.Series, asof: date, s: dict,
                 long_window: bool) -> dict:
    """All Step A numbers for one member's transactions (all asset types)."""
    start = asof - timedelta(days=int(s["window_days"]))
    win = tx[(tx["trade_date"] >= start) & (tx["trade_date"] <= asof)]
    eq = win[win["asset_type"].isin(["ST", "OP"]) & win["ticker"].notna()]
    stocks = tx[(tx["asset_type"] == "ST") & tx["ticker"].notna() & tx["type"].isin(["P", "S"])]
    orig = copy_trade(stocks, prices, spy, start, asof, delayed=False)
    dly = copy_trade(stocks, prices, spy, start, asof, delayed=True)
    delays = (pd.to_datetime(win["filing_date"]) - pd.to_datetime(win["trade_date"])).dt.days.dropna()
    notes = " ".join(win["filing_notes"].fillna("").unique()).lower()
    out = {
        "n_trades": len(eq), "n_buys": int((eq["type"] == "P").sum()),
        "ret_1y": orig["ret"], "spy_1y": orig["spy_ret"], "excess_1y": orig["excess"],
        "ret_1y_delay": dly["ret"], "excess_1y_delay": dly["excess"],
        "reliability_gap_pp": abs(orig["ret"] - dly["ret"]) * 100 if orig["capital"] and dly["capital"] else np.nan,
        "excess_ex_best": orig["excess_ex_best"], "best_ticker": orig["best_ticker"],
        "capital_1y": orig["capital"], "est_portfolio": orig["open_value"],
        "est_portfolio_low": orig["open_value_low"], "est_portfolio_high": orig["open_value_high"],
        "top_weight": orig["top_weight"], "median_filing_delay": float(delays.median()) if len(delays) else np.nan,
        "late_share": float((delays > int(s["late_filing_days"])).mean()) if len(delays) else np.nan,
        "options_share": float((eq["asset_type"] == "OP").mean()) if len(eq) else 0.0,
        "managed_or_blind": any(k in notes for k in ("blind trust", "managed account", "discretionary", "managed by")),
        "unpriced_trades": orig["unpriced"], "ret_3y": np.nan, "excess_3y": np.nan,
    }
    if long_window:
        lw = copy_trade(stocks, prices, spy, asof - timedelta(days=int(s["window_days_long"])), asof, delayed=False)
        out["ret_3y"], out["excess_3y"] = lw["ret"], lw["excess"]
    return out


def apply_criteria(st: dict, s: dict) -> tuple[bool, list[str], list[str], str | None]:
    """Returns (qualified, fail_reasons, flags, exception_reason)."""
    fails, flags, exception = [], [], None
    ex = st["excess_1y"]
    if pd.isna(ex):
        fails.append("no priced stock purchases in the window")
    elif ex <= 0:
        fails.append(f"trails SPY by {-ex:.1%} (copy-trade, 1Y)")
    gap = st["reliability_gap_pp"]
    if pd.notna(gap) and gap > float(s["reliability_max_gap_pp"]):
        fails.append(f"unreliable: return differs {gap:.1f}pp from delay-adjusted (> {s['reliability_max_gap_pp']}pp)")
    if st["n_trades"] < int(s["min_trades"]):
        fails.append(f"{st['n_trades']} trades (< {s['min_trades']})")
    if pd.notna(ex) and ex > 0 and not (st["excess_ex_best"] > 0):
        fails.append(f"beats SPY only because of {st['best_ticker']}")
    if pd.notna(st["median_filing_delay"]) and st["median_filing_delay"] > float(s["max_median_filing_delay"]):
        fails.append(f"median filing delay {st['median_filing_delay']:.0f}d (> {s['max_median_filing_delay']})")
    if pd.notna(st["late_share"]) and st["late_share"] > float(s["max_late_share"]):
        fails.append(f"chronic late filer: {st['late_share']:.0%} of trades filed after {s['late_filing_days']}d")

    need = float(s["min_portfolio_usd"])
    lo, hi = st["est_portfolio_low"], st["est_portfolio_high"]
    if st["est_portfolio"] >= need:
        if lo < need:
            flags.append("portfolio borderline: amount ranges straddle $1M")
    elif hi >= need:
        flags.append("portfolio borderline: amount ranges straddle $1M")
    else:
        if pd.notna(ex) and ex * 100 >= float(s["exception_min_excess_pp"]):
            exception = f"kept despite est. portfolio ${st['est_portfolio']:,.0f}: beats SPY by {ex:.1%}"
        elif pd.notna(st["top_weight"]) and st["top_weight"] >= float(s["exception_min_top_weight"]):
            exception = (f"kept despite est. portfolio ${st['est_portfolio']:,.0f}: unusual allocation "
                         f"(top position {st['top_weight']:.0%} of the book)")
        else:
            fails.append(f"est. PTR-traded portfolio ${st['est_portfolio']:,.0f} (< ${need:,.0f})")

    if st["managed_or_blind"]:
        flags.append("managed account / blind trust language in filings")
    if st["options_share"] >= float(s["options_heavy_share"]):
        flags.append(f"options-heavy ({st['options_share']:.0%} of trades)")
    return not fails, fails, flags, exception
