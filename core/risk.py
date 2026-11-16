"""risk-and-sizing: paper position sizes and portfolio limits for Signals. Research only; places no orders.

Rules (all thresholds in settings `risk:`):
  - size = (account x risk_per_trade_pct x size_multiplier) / (entry - stop), floored to whole shares,
    capped at max_position_pct of the account
  - reject below min_reward_risk (2:1), beyond max_open_positions, beyond max_per_sector,
    or (if correlation_action = reject) when daily returns correlate above max_correlation with a position
  - existing positions = taken, still-active trades in the paper journal
Ideas are processed best score first; each approval joins the book, so limits apply to the batch too.

    uv run python -m core.risk --set-account 50000
    uv run python -m core.risk --signals reports/breakout-2026-11-16.json [--log]
"""

from __future__ import annotations

import argparse
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from core.config import Settings, set_local, settings as default_settings
from core.signals import Signal, load_signals


@dataclass
class SizingDecision:
    ticker: str
    skill: str
    score: float
    approved: bool
    reward_risk: float
    entry_price: float | None = None     # price used for sizing (top of entry zone by default)
    stop: float | None = None
    shares: int | None = None
    notional: float | None = None
    risk_usd: float | None = None
    risk_pct_of_account: float | None = None
    sector: str | None = None
    max_corr: float | None = None
    corr_with: str | None = None
    reasons: list[str] = field(default_factory=list)   # why rejected
    flags: list[str] = field(default_factory=list)     # caveats that don't reject


def position_size(account: float, risk_pct: float, entry: float, stop: float, max_position_pct: float,
                  multiplier: float = 1.0) -> tuple[int, bool]:
    """Whole shares risking at most account*risk_pct*multiplier between entry and stop, capped at
    max_position_pct of the account. Returns (shares, capped_by_position_limit)."""
    per_share = entry - stop
    if per_share <= 0 or entry <= 0:
        raise ValueError(f"entry {entry} must be above stop {stop}")
    by_risk = math.floor(account * risk_pct * multiplier / per_share)
    by_cap = math.floor(account * max_position_pct / entry)
    return (by_cap, True) if by_cap < by_risk else (by_risk, False)


def max_correlation(candidate: pd.Series | None, book: dict[str, pd.Series], lookback: int
                    ) -> tuple[float | None, str | None]:
    """Highest correlation of daily returns between the candidate and any book position over `lookback` days."""
    if candidate is None or not book:
        return None, None
    best, who = None, None
    for t, r in book.items():
        if r is None:
            continue
        a, b = candidate.align(r, join="inner")
        a, b = a.tail(lookback), b.tail(lookback)
        if len(a) < max(20, lookback // 2):
            continue
        c = float(a.corr(b))
        if pd.notna(c) and (best is None or c > best):
            best, who = c, t
    return best, who


def size_signals(signals: Iterable[Signal], cfg: Settings, existing: list[dict] | None = None,
                 sector_of: Callable[[str], str | None] | None = None,
                 returns: dict[str, pd.Series] | None = None, account: float | None = None,
                 multiplier: float | None = None) -> list[SizingDecision]:
    """Size and filter ideas. `existing` = [{"ticker", "sector"}] already in the book (journal)."""
    r = cfg.section("risk")
    account = account if account is not None else cfg.get("account.size_usd", None)
    multiplier = float(multiplier if multiplier is not None else r["size_multiplier"])
    sector_of = sector_of or (lambda t: None)
    returns = returns or {}
    book = [dict(e) for e in (existing or [])]

    decisions: list[SizingDecision] = []
    for s in sorted(signals, key=lambda x: x.score, reverse=True):
        entry = s.entry_zone.high if r["entry_for_sizing"] == "high" else s.entry_mid
        d = SizingDecision(ticker=s.ticker, skill=s.skill, score=s.score, approved=False,
                           reward_risk=round(s.reward_risk, 2), entry_price=round(entry, 2), stop=s.stop)

        # Earnings plays whose targets are typical earnings moves can't reach 2:1 by construction; they qualify
        # on their historical odds (enforced by the catalyst skill's play rules) and are sized smaller instead.
        setup = str(s.metrics.get("setup") or "")
        exempt = setup in set(r.get("rr_exempt_setups") or [])
        idea_mult = multiplier * (float(r["rr_exempt_size_multiplier"]) if exempt else 1.0)
        if exempt:
            d.flags.append(f"R:R rule exempt ({setup}, R:R {s.reward_risk:.2f}): qualified on historical odds, "
                           f"sized at {float(r['rr_exempt_size_multiplier']):.0%}")
        elif round(s.reward_risk, 2) < float(r["min_reward_risk"]):   # cents rounding can't fail an exact 2R plan
            d.reasons.append(f"R:R {s.reward_risk:.2f} < {float(r['min_reward_risk']):g}")
        if any(p["ticker"] == s.ticker for p in book):
            d.reasons.append("already in the book (open or pending)")
        if len(book) >= int(r["max_open_positions"]):
            d.reasons.append(f"max open positions ({r['max_open_positions']}) reached")

        d.sector = sector_of(s.ticker)
        if d.sector:
            same = sum(1 for p in book if p.get("sector") == d.sector)
            if same >= int(r["max_per_sector"]):
                d.reasons.append(f"sector cap: {same} {d.sector} positions (max {r['max_per_sector']})")
        else:
            d.flags.append("sector unknown: sector cap not applied")

        book_returns = {p["ticker"]: returns.get(p["ticker"]) for p in book}
        if book:
            d.max_corr, d.corr_with = max_correlation(returns.get(s.ticker), book_returns,
                                                      int(r["correlation_lookback_days"]))
            if d.max_corr is None:
                d.flags.append("correlation unchecked (insufficient price data)")
            elif d.max_corr > float(r["max_correlation"]):
                msg = f"correlation {d.max_corr:.2f} with {d.corr_with} > {float(r['max_correlation']):g}"
                (d.reasons if r["correlation_action"] == "reject" else d.flags).append(msg)
            d.max_corr = round(d.max_corr, 3) if d.max_corr is not None else None

        if account:
            shares, capped = position_size(float(account), float(r["risk_per_trade_pct"]), entry, s.stop,
                                           float(r["max_position_pct"]), idea_mult)
            d.shares = shares
            d.notional = round(shares * entry, 2)
            d.risk_usd = round(shares * (entry - s.stop), 2)
            d.risk_pct_of_account = round(d.risk_usd / float(account), 5)
            if capped:
                d.flags.append(f"size capped at {float(r['max_position_pct']):.0%} of account (tight stop)")
            if shares < int(r["min_shares"]):
                d.reasons.append("position rounds to < 1 share at this risk budget")
        else:
            d.flags.append("account size not set: no share count (run --set-account)")
        if multiplier != 1.0:
            d.flags.append(f"size multiplier {multiplier:g} applied")

        d.approved = not d.reasons
        if d.approved:
            book.append({"ticker": s.ticker, "sector": d.sector})
        decisions.append(d)
    return decisions


def decisions_table(decisions: list[SizingDecision]) -> pd.DataFrame:
    rows = []
    for d in decisions:
        row = asdict(d)
        row["reasons"] = "; ".join(d.reasons)
        row["flags"] = "; ".join(d.flags)
        rows.append(row)
    return pd.DataFrame(rows)


def write_report(decisions: list[SizingDecision], cfg: Settings, account: float | None, sources: list[str]) -> Path:
    r = cfg.section("risk")
    out = cfg.path_for("paths.reports_dir") / f"sizing-{date.today().isoformat()}.md"
    acct = f"${account:,.0f}" if account else "NOT SET"
    lines = [
        f"# Risk and sizing — {date.today().isoformat()}",
        "",
        "_Paper sizing for research. Not investment advice; no orders are placed._",
        "",
        f"- Account: {acct}; risk per idea {float(r['risk_per_trade_pct']):.2%} x multiplier "
        f"{float(r['size_multiplier']):g}; max position {float(r['max_position_pct']):.0%}; "
        f"min R:R {float(r['min_reward_risk']):g}; max {r['max_open_positions']} open, {r['max_per_sector']} per sector",
        f"- Signals: {', '.join(sources)}",
        "",
        "| Ticker | Skill | Score | OK | R:R | Entry | Stop | Shares | Notional | Risk $ | Sector | Max corr | Reasons / flags |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for d in decisions:
        corr = f"{d.max_corr:.2f} ({d.corr_with})" if d.max_corr is not None else "—"
        notes = "; ".join(d.reasons + d.flags) or "—"
        lines.append(
            f"| {d.ticker} | {d.skill} | {d.score:.0f} | {'yes' if d.approved else 'no'} | {d.reward_risk:.2f} | "
            f"{d.entry_price:.2f} | {d.stop:.2f} | {d.shares if d.shares is not None else '—'} | "
            f"{f'${d.notional:,.0f}' if d.notional is not None else '—'} | "
            f"{f'${d.risk_usd:,.0f}' if d.risk_usd is not None else '—'} | {d.sector or '?'} | {corr} | {notes} |")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> None:
    from core import journal
    from core.providers.yfinance_provider import YFinanceProvider

    ap = argparse.ArgumentParser(description="Paper position sizing and portfolio limits (research only).")
    ap.add_argument("--set-account", type=float, metavar="USD", help="store account size in config/local.yaml")
    ap.add_argument("--signals", nargs="+", type=Path, help="Signal JSON file(s) to size")
    ap.add_argument("--log", action="store_true", help="log every idea to the paper journal (approved = taken)")
    ap.add_argument("--multiplier", type=float, help="override size multiplier (e.g. 0.5 in a weak regime)")
    args = ap.parse_args(argv)

    if args.set_account is not None:
        if args.set_account <= 0:
            ap.error("account size must be positive")
        p = set_local("account.size_usd", float(args.set_account))
        print(f"Account size ${args.set_account:,.0f} saved to {p} (gitignored).")
        if not args.signals:
            return
    if not args.signals:
        ap.error("--signals is required (or use --set-account)")

    cfg = default_settings()
    prov = YFinanceProvider(cfg)
    jpath = journal.db_path(cfg)
    journal.update_outcomes(jpath, prov, cfg)          # so closed trades free up their slots
    book = journal.open_positions(jpath)
    sigs = [s for f in args.signals for s in load_signals(f)]
    # Re-sizing ideas that are already journaled must not conflict with themselves.
    same = {(journal.signal_date_of(s).isoformat(), s.ticker, s.skill) for s in sigs}
    if len(book):
        book = book[[(d, t, k) not in same for d, t, k in zip(book["signal_date"], book["ticker"], book["skill"])]]
    tickers = sorted({s.ticker for s in sigs} | set(book["ticker"]))
    hist = prov.history(tickers, "1y") if tickers else {}
    rets = {t: df["close"].pct_change().dropna() for t, df in hist.items()}
    existing = [{"ticker": t, "sector": prov.sector(t)} for t in book["ticker"]]
    decisions = size_signals(sigs, cfg, existing, prov.sector, rets, multiplier=args.multiplier)

    account = cfg.get("account.size_usd", None)
    rep = write_report(decisions, cfg, account, [str(f) for f in args.signals])
    t = decisions_table(decisions)
    pd.set_option("display.width", 220)
    print(t[["ticker", "approved", "reward_risk", "entry_price", "stop", "shares", "notional", "risk_usd",
             "sector", "max_corr", "reasons", "flags"]].to_string(index=False))
    print(f"\nExisting book: {len(book)} active paper trades. Report: {rep}")
    if args.log:
        res = journal.log_signals(jpath, sigs, {d.ticker: d for d in decisions},
                                  log_rejected=bool(cfg.get("journal.log_rejected", True)))
        print(f"Journal: {res['inserted']} logged, {res['skipped_duplicates']} already present ({jpath}).")


if __name__ == "__main__":
    main()
