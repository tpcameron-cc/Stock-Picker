"""Paper-trade journal: log every idea, mark outcomes from daily prices, measure performance.

Storage: SQLite at settings `paths.journal` (table `paper_trades`), queryable with plain SQL.
Outcomes use core.simulate (the same fill/exit rules as the backtest), recomputed from price data on
every update until a trade is final (closed or expired). Nothing here places orders.

    uv run python -m core.journal update      # mark outcomes for pending/open trades
    uv run python -m core.journal summary     # performance per skill and per signal type
    uv run python -m core.journal list [--all]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import numpy as np
import pandas as pd

from core.config import Settings, settings as default_settings
from core.signals import Signal
from core.providers.yfinance_provider import NY
from core.simulate import entry_mode, simulate_trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT,
    logged_at     TEXT,
    signal_date   TEXT NOT NULL,     -- close the idea was decided on (price as-of)
    ticker        TEXT NOT NULL,
    company       TEXT,
    skill         TEXT NOT NULL,
    setup         TEXT,
    score         REAL,
    confidence    TEXT,
    signals_hit   TEXT,              -- JSON list
    entry_low     REAL NOT NULL,
    entry_high    REAL NOT NULL,
    stop          REAL NOT NULL,
    target        REAL NOT NULL,
    horizon_days  INTEGER NOT NULL,
    entry_mode    TEXT,              -- buy_stop | next_open
    cancel_below  REAL,
    taken         INTEGER NOT NULL,  -- 1 = approved by risk-and-sizing, 0 = rejected (tracked for comparison)
    shares        INTEGER,
    risk_usd      REAL,
    sizing_notes  TEXT,
    status        TEXT NOT NULL,     -- pending | open | closed | expired
    fill_date     TEXT,
    fill_price    REAL,
    exit_date     TEXT,
    exit_price    REAL,
    exit_reason   TEXT,              -- stop | trail_stop | target | time | (open = marked to market)
    r_multiple    REAL,              -- realized, or unrealized while open
    return_pct    REAL,
    spy_return    REAL,              -- SPY over the same dates
    last_update   TEXT,
    signal_json   TEXT,
    UNIQUE (signal_date, ticker, skill)
);
"""
FINAL = ("closed", "expired")


def db_path(cfg: Settings | None = None) -> Path:
    return (cfg or default_settings()).path_for("paths.journal")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(SCHEMA)
    return con


def signal_date_of(sig: Signal) -> date:
    v = sig.metrics.get("price_as_of")
    if v:
        return date.fromisoformat(str(v))
    return min(s.as_of for s in sig.sources)


def log_signals(path: str | Path, signals: Iterable[Signal], decisions: dict | None = None,
                run_id: str | None = None, log_rejected: bool = True) -> dict:
    """Insert ideas. `decisions` maps ticker or (ticker, skill) -> SizingDecision (core.risk). Ideas without a decision are logged
    as taken=0 with a note. Re-logging the same (signal_date, ticker, skill) is ignored."""
    decisions = decisions or {}
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = skipped = 0
    with closing(connect(path)) as con, con:
        for s in signals:
            d = decisions.get((s.ticker, s.skill), decisions.get(s.ticker))   # per-skill decision wins
            taken = int(bool(d and d.approved))
            if not taken and not log_rejected:
                continue
            notes = "; ".join((d.reasons + d.flags) if d else ["not sized"])
            cur = con.execute(
                """INSERT OR IGNORE INTO paper_trades (run_id, logged_at, signal_date, ticker, company, skill, setup,
                   score, confidence, signals_hit, entry_low, entry_high, stop, target, horizon_days, entry_mode,
                   cancel_below, taken, shares, risk_usd, sizing_notes, status, signal_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, now, signal_date_of(s).isoformat(), s.ticker, s.company, s.skill,
                 str(s.metrics.get("setup") or ""), s.score, s.confidence.value, json.dumps(s.signals_hit),
                 s.entry_zone.low, s.entry_zone.high, s.stop, s.targets[0], s.horizon_days,
                 s.metrics.get("entry_mode"), s.metrics.get("cancel_below"), taken,
                 d.shares if d else None, d.risk_usd if d else None, notes, "pending", s.model_dump_json()))
            inserted += cur.rowcount
            skipped += 1 - cur.rowcount
    return {"inserted": inserted, "skipped_duplicates": skipped, "run_id": run_id}


def read(path: str | Path, where: str = "1=1", params: tuple = ()) -> pd.DataFrame:
    with closing(connect(path)) as con:
        return pd.read_sql_query(f"SELECT * FROM paper_trades WHERE {where} ORDER BY signal_date, id", con,
                                 params=params)


def open_positions(path: str | Path) -> pd.DataFrame:
    """Taken trades that still occupy risk budget: pending (order working) or open."""
    return read(path, "taken = 1 AND status IN ('pending', 'open')")


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------

MARKET_OPEN = dtime(9, 30)


def earliest_fill_index(index: pd.DatetimeIndex, logged_at) -> int:
    """First daily bar a paper order could really have been placed for, given when the idea was LOGGED:
    logged before 9:30 ET -> that day's session can fill; logged later (e.g. an evening run) -> the next session.
    Missing timestamp -> no restriction (0)."""
    if logged_at is None or (isinstance(logged_at, float) and pd.isna(logged_at)) or logged_at == "":
        return 0
    ts = pd.Timestamp(logged_at)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    et = ts.tz_convert(NY)
    day = pd.Timestamp(et.date())
    return int(index.searchsorted(day, side="left" if et.time() < MARKET_OPEN else "right"))


def evaluate_row(row: dict, df: pd.DataFrame, spy_close: pd.Series | None, pending_days: int,
                 slippage: float, same_bar_rule: str) -> dict:
    """Recompute one trade's state from daily bars `df` (must include the signal date). Pure function.
    Fills can't happen before the first session after the idea was logged (see earliest_fill_index)."""
    sd = pd.Timestamp(row["signal_date"])
    pos = df.index.searchsorted(sd, side="right") - 1
    if pos < 0 or df.index[pos].normalize() != sd.normalize():
        return {"status": row["status"], "note": f"signal date {row['signal_date']} not in price history"}
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    plan = SimpleNamespace(setup=row["setup"] or "", entry_low=row["entry_low"], entry_high=row["entry_high"],
                           stop=row["stop"], target=row["target"], cancel_below=row["cancel_below"],
                           entry_mode=row["entry_mode"])
    earliest = earliest_fill_index(df.index, row.get("logged_at"))
    res = simulate_trade(o, h, l, c, pos, plan, pending_days, int(row["horizon_days"]), slippage, same_bar_rule,
                         earliest_fill=earliest)
    out = {"fill_date": None, "fill_price": None, "exit_date": None, "exit_price": None, "exit_reason": None,
           "r_multiple": None, "return_pct": None, "spy_return": None}
    if res is None:
        start = max(pos + 1, earliest)
        window = pending_days if entry_mode(plan) == "buy_stop" else 1   # next-open orders get one chance
        out["status"] = "expired" if len(df) - start >= window else "pending"
        return out
    fi, xi, fpx, xpx, reason = res
    risk = fpx - row["stop"]
    out.update(fill_date=df.index[fi].date().isoformat(), fill_price=float(fpx),
               r_multiple=float((xpx - fpx) / risk) if risk > 0 else None, return_pct=float(xpx / fpx - 1))
    if reason == "open_at_end":
        out.update(status="open", exit_reason="open")
    else:
        out.update(status="closed", exit_date=df.index[xi].date().isoformat(), exit_price=float(xpx),
                   exit_reason=reason)
    if spy_close is not None and fi > 0:
        d0, d1 = df.index[fi - 1], df.index[xi]
        if d0 in spy_close.index and d1 in spy_close.index:
            out["spy_return"] = float(spy_close[d1] / spy_close[d0] - 1)
    return out


def update_outcomes(path: str | Path, price_provider=None, cfg: Settings | None = None) -> pd.DataFrame:
    """Re-evaluate every non-final trade from fresh daily prices. Returns the updated rows."""
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    j = cfg.section("journal")
    live = read(path, "status NOT IN ('closed', 'expired')")
    if live.empty:
        return live
    price_provider = price_provider or YFinanceProvider(cfg)
    bench = cfg.get("indicators.benchmark")
    hist = price_provider.history(sorted(set(live["ticker"]) | {bench}), "1y")
    spy = hist.get(bench)
    spy_close = spy["close"] if spy is not None else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updates = []
    with closing(connect(path)) as con, con:
        for row in live.to_dict("records"):
            df = hist.get(row["ticker"])
            if df is None or df.empty:
                continue
            out = evaluate_row(row, df, spy_close, int(j["pending_days"]), float(j["slippage_pct"]),
                               j["same_bar_rule"])
            out.pop("note", None)
            cols = ", ".join(f"{k} = ?" for k in out) + ", last_update = ?"
            con.execute(f"UPDATE paper_trades SET {cols} WHERE id = ?", (*out.values(), now, row["id"]))
            updates.append({"id": row["id"], "ticker": row["ticker"], **out})
    return pd.DataFrame(updates)


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

def trade_stats(t: pd.DataFrame) -> dict:
    """Stats over CLOSED trades. Returns are fractions; R uses the initial stop distance."""
    if t.empty:
        return {"trades": 0}
    wins, losses = t[t["return_pct"] > 0], t[t["return_pct"] <= 0]
    spy = t["spy_return"]
    return {
        "trades": len(t),
        "win_rate": len(wins) / len(t),
        "avg_win": wins["return_pct"].mean() if len(wins) else np.nan,
        "avg_loss": losses["return_pct"].mean() if len(losses) else np.nan,
        "expectancy_pct": t["return_pct"].mean(),
        "expectancy_r": t["r_multiple"].mean(),
        "avg_spy": spy.mean() if spy.notna().any() else np.nan,
        "excess_vs_spy": (t["return_pct"] - spy).mean() if spy.notna().any() else np.nan,
        "stops": int(t["exit_reason"].isin(["stop", "trail_stop"]).sum()),
        "targets": int((t["exit_reason"] == "target").sum()),
        "time_stops": int((t["exit_reason"] == "time").sum()),
    }


def performance(trades: pd.DataFrame, taken_only: bool = True) -> dict[str, pd.DataFrame]:
    """Closed-trade stats per skill, per setup and per signal type (one trade counts under each signal it hit)."""
    t = trades[trades["status"] == "closed"]
    if taken_only:
        t = t[t["taken"] == 1]
    out = {"overall": pd.DataFrame({"ALL": trade_stats(t)}).T}
    if t.empty:
        return out
    out["by_skill"] = pd.DataFrame({k: trade_stats(g) for k, g in t.groupby("skill")}).T
    out["by_setup"] = pd.DataFrame({f"{s}/{k}": trade_stats(g) for (s, k), g in t.groupby(["skill", "setup"])}).T
    ex = t.assign(signal=t["signals_hit"].map(lambda x: json.loads(x) if x else [])).explode("signal")
    ex = ex[ex["signal"].notna()]
    out["by_signal"] = pd.DataFrame({k: trade_stats(g) for k, g in ex.groupby("signal")}).T
    return out


def status_counts(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    return trades.pivot_table(index="skill", columns="status", values="id", aggfunc="count", fill_value=0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_tables(tables: dict[str, pd.DataFrame]) -> None:
    pd.set_option("display.width", 200)
    for name, df in tables.items():
        print(f"\n== {name} ==")
        print(df.to_string(float_format=lambda v: f"{v:.4f}"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Paper-trade journal (research only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update", help="mark outcomes for pending/open trades from daily prices")
    s = sub.add_parser("summary", help="performance of closed trades")
    s.add_argument("--include-rejected", action="store_true", help="include ideas risk-and-sizing rejected")
    lst = sub.add_parser("list", help="list trades")
    lst.add_argument("--all", action="store_true", help="include closed/expired")
    args = ap.parse_args(argv)

    path = db_path()
    if args.cmd == "update":
        upd = update_outcomes(path)
        print(f"Updated {len(upd)} trades." if len(upd) else "Nothing to update.")
        if len(upd):
            print(upd[["ticker", "status", "fill_date", "exit_reason", "r_multiple"]].to_string(index=False))
    elif args.cmd == "summary":
        t = read(path)
        print(f"Journal: {path}  ({len(t)} ideas logged)")
        print(status_counts(t).to_string() if len(t) else "empty")
        _print_tables(performance(t, taken_only=not args.include_rejected))
        n_closed = int(((t["status"] == "closed") & (t["taken"] == 1)).sum()) if len(t) else 0
        if n_closed < 30:
            print(f"\nOnly {n_closed} closed taken trades: too few to judge any skill yet (aim for 30+ per skill).")
    else:
        t = read(path) if args.all else read(path, "status NOT IN ('closed', 'expired')")
        cols = ["id", "signal_date", "ticker", "skill", "setup", "taken", "shares", "status", "fill_date",
                "fill_price", "stop", "target", "exit_reason", "r_multiple"]
        print(t[cols].to_string(index=False) if len(t) else "No trades.")


if __name__ == "__main__":
    main()
