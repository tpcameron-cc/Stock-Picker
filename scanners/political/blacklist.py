"""Sell blacklist: a big sale by a qualifying member blocks the stock from the daily list for N days.

"Big" (user's rule, 2026-11-17): the reported sale range starts above $15,000 AND the sale is >= 75% of the
member's position in that stock. Disclosures give dollar ranges, not holdings, so the position is ESTIMATED from the
member's own disclosed trades (range midpoints, cost basis): buys add, sales subtract. A sale reported as a FULL sale
counts as 100%. A partial sale with no estimable position (bought before the data window) does not trigger.
Pure functions; the political scan writes the result to `political.blacklist.file` for run-all-scans.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from scanners.political.screen import amount_mid


def sale_fractions(trades: pd.DataFrame) -> pd.DataFrame:
    """For one member's trades in one stock, the estimated share of the position each sale represents.
    Adds columns est_position_before (USD) and sale_fraction (None when it can't be estimated)."""
    t = trades.sort_values(["trade_date", "type"]).copy()        # P before S on the same day
    held = 0.0
    before, frac = [], []
    for r in t.itertuples():
        mid = amount_mid(r.amount_low, r.amount_high) or 0.0
        if r.type == "P":
            held += mid
            before.append(None)
            frac.append(None)
        elif r.type == "S":
            before.append(held if held > 0 else None)
            full = not bool(getattr(r, "partial", False))
            if full:
                frac.append(1.0)
                held = 0.0
            else:
                frac.append(min(mid / held, 1.0) if held > 0 else None)
                held = max(held - mid, 0.0)
        else:
            before.append(None)
            frac.append(None)
    t["est_position_before"] = before
    t["sale_fraction"] = frac
    return t


def build_blacklist(tx: pd.DataFrame, members: set[str], asof: date, b: dict, names: dict | None = None) -> pd.DataFrame:
    """Blacklist entries active on `asof`: one row per ticker (the latest-ending entry wins).
    `tx`: transactions with bioguide, ticker, type, partial, trade_date, filing_date, amount_low, amount_high,
    asset_type, source_url. `members`: bioguide ids whose sales count."""
    cols = ["ticker", "member", "bioguide", "trade_date", "filing_date", "amount", "full_sale", "sale_fraction",
            "est_position_before", "until", "source_url"]
    names = names or {}
    if tx.empty:
        return pd.DataFrame(columns=cols)
    days = int(b["days"])
    stocks = tx[(tx["asset_type"] == "ST") & tx["ticker"].notna() & tx["bioguide"].isin(members)]
    rows = []
    for (bg, tk), g in stocks.groupby(["bioguide", "ticker"]):
        f = sale_fractions(g)
        s = f[(f["type"] == "S") & (f["trade_date"] >= asof - timedelta(days=days)) & (f["trade_date"] <= asof)]
        for r in s.itertuples():
            if not (r.amount_low is not None and r.amount_low >= float(b["min_amount_low_usd"])):
                continue
            if r.sale_fraction is None or r.sale_fraction < float(b["min_fraction"]):
                continue
            hi = f"${r.amount_high:,.0f}" if r.amount_high is not None and not pd.isna(r.amount_high) else "over"
            rows.append({"ticker": tk, "member": names.get(bg, bg), "bioguide": bg, "trade_date": r.trade_date,
                         "filing_date": r.filing_date, "amount": f"${r.amount_low:,.0f}–{hi}",
                         "full_sale": not bool(r.partial), "sale_fraction": round(float(r.sale_fraction), 3),
                         "est_position_before": None if r.est_position_before is None else round(float(r.est_position_before)),
                         "until": r.trade_date + timedelta(days=days), "source_url": r.source_url})
    out = pd.DataFrame(rows, columns=cols)
    if out.empty:
        return out
    out = out[out["until"] > asof]
    return out.sort_values("until").groupby("ticker", as_index=False).last().sort_values("until", ascending=False)


def save_blacklist(bl: pd.DataFrame, path: Path, asof: date) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    recs = json.loads(bl.to_json(orient="records", date_format="iso")) if len(bl) else []
    for r in recs:
        for k in ("trade_date", "filing_date", "until"):
            if r.get(k):
                r[k] = str(r[k])[:10]
    path.write_text(json.dumps({"as_of": asof.isoformat(), "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "entries": recs}, indent=1), encoding="utf-8")
    return path


def load_blacklist(path: Path, asof: date) -> tuple[dict[str, dict], str | None]:
    """Active entries keyed by ticker, and the as-of date of the saved list (None if there is no list)."""
    if not path.exists():
        return {}, None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, None
    active = {e["ticker"]: e for e in d.get("entries", []) if date.fromisoformat(e["until"]) > asof}
    return active, d.get("as_of")


def reason(e: dict) -> str:
    what = "sold the whole position" if e.get("full_sale") else f"sold ~{e['sale_fraction']:.0%} of the estimated position"
    return (f"blacklisted until {e['until']}: {e['member']} {what} ({e['amount']}) on {e['trade_date']} "
            f"(filed {e['filing_date']})")
