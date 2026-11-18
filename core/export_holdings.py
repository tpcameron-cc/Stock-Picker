"""Export the paper portfolio's current holdings to JSON for the Paper Portfolio dashboard.

    uv run python -m core.export_holdings            # writes data/exports/holdings.json

Holdings = journal ideas that risk-and-sizing TOOK (taken = 1) and that are still pending (waiting for entry) or
open (filled). Each row is marked to the latest daily close Yahoo has published. The JSON is {rows, meta}; meta
carries this script's own source so the numbers can be checked and rerun.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core import journal
from core.config import PROJECT_ROOT, settings as default_settings


def _num(x, nd=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def build_rows(t: pd.DataFrame, hist: dict, sectors: dict, account: float | None) -> list[dict]:
    rows = []
    for r in t.itertuples():
        df = hist.get(r.ticker)
        last = float(df["close"].iloc[-1]) if df is not None and len(df) else None
        last_date = df.index[-1].date().isoformat() if df is not None and len(df) else None
        shares = int(r.shares) if pd.notna(r.shares) else 0
        filled = r.status == "open" and pd.notna(r.fill_price)
        ref = float(r.fill_price) if filled else float(r.entry_high)          # pending: worst-case entry
        value = shares * (last if (filled and last is not None) else ref)
        risk_per_share = ref - float(r.stop)
        unreal = (last - ref) * shares if filled and last is not None else None
        rows.append({
            "ticker": r.ticker, "company": json.loads(r.signal_json).get("company", r.ticker) if r.signal_json else r.ticker,
            "skill": r.skill, "setup": r.setup, "status": "filled" if filled else "waiting for entry",
            "signal_date": r.signal_date, "logged": str(r.logged_at)[:10], "shares": shares,
            "entry_low": _num(r.entry_low, 2), "entry_high": _num(r.entry_high, 2), "stop": _num(r.stop, 2),
            "target": _num(r.target, 2), "fill_date": r.fill_date if pd.notna(r.fill_date) else None, "fill_price": _num(r.fill_price, 2),
            "last_price": _num(last, 2), "last_price_date": last_date, "position_value": _num(value, 2),
            "risk_usd": _num(risk_per_share * shares, 2), "planned_risk_usd": _num(r.risk_usd, 2),
            "risk_pct_of_account": _num(risk_per_share * shares / account, 5) if account else None,
            "unrealized_usd": _num(unreal, 2), "unrealized_r": _num((last - ref) / risk_per_share, 3)
            if filled and last is not None and risk_per_share > 0 else None,
            "to_stop_pct": _num(float(r.stop) / last - 1, 4) if last else None,
            "to_target_pct": _num(float(r.target) / last - 1, 4) if last else None,
            "to_entry_pct": _num(float(r.entry_low) / last - 1, 4) if last and not filled else None,
            "horizon_days": int(r.horizon_days), "sector": sectors.get(r.ticker) or "Unknown",
            "account_usd": _num(account, 2), "journal_id": int(r.id),
        })
    return rows


def main() -> None:
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = default_settings()
    prov = YFinanceProvider(cfg)
    jpath = journal.db_path(cfg)
    journal.update_outcomes(jpath, prov, cfg)
    t = journal.read(jpath, "taken = 1 AND status IN ('pending', 'open')")
    tickers = sorted(set(t["ticker"]))
    hist = prov.history(tickers, "1y") if tickers else {}
    sectors = {tk: prov.sector(tk) for tk in tickers}
    account = cfg.get("account.size_usd", None)
    rows = build_rows(t, hist, sectors, float(account) if account else None)
    out = PROJECT_ROOT / "data" / "exports" / "holdings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = [
        {"label": "export_holdings.py", "value": Path(__file__).read_text(encoding="utf-8"), "type": "code", "lang": "python"},
        {"label": "Input", "value": "data/journal.sqlite (paper_trades, taken = 1, pending or open), Yahoo daily closes"},
        {"label": "Exported", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
    ]
    out.write_text(json.dumps({"rows": rows, "meta": meta}, indent=1, allow_nan=False), encoding="utf-8")
    print(f"{len(rows)} holdings -> {out}")


if __name__ == "__main__":
    main()
