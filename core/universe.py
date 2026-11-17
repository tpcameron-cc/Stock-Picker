"""Build the tradable universe: exchange-listed common stocks that pass liquidity filters,
with dilution / pump-and-dump risk flags attached.

Run:  uv run python -m core.universe [--source list|nasdaqtrader] [--limit N] [--edgar]
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from core import indicators as ind
from core.config import Settings, settings as default_settings

log = logging.getLogger(__name__)


@dataclass
class UniverseResult:
    table: pd.DataFrame                       # one row per candidate, passed + rejected
    sources: list[dict] = field(default_factory=list)  # {name, url, as_of}
    histories: dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)  # reuse; avoids refetch
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def passed(self) -> pd.DataFrame:
        return self.table[self.table["passed"]]

    @property
    def tickers(self) -> list[str]:
        return self.passed["symbol"].tolist()


# ---------- candidate list ----------

def select_candidates(directory: pd.DataFrame, cfg: Settings, only: list[str] | None = None) -> pd.DataFrame:
    """Filter the symbol directory to candidates. If `only` is given, keep those symbols and
    mark any not found in the directory (likely OTC or delisted) instead of dropping them silently."""
    u = cfg.section("universe")
    d = directory.copy()
    if only is not None:
        wanted = pd.DataFrame({"symbol": [s.upper() for s in only]})
        d = wanted.merge(d, on="symbol", how="left")
        d["in_directory"] = d["exchange"].notna()
        d["name"] = d["name"].fillna("")
        d[["is_etf", "is_test"]] = d[["is_etf", "is_test"]].fillna(False).astype(bool)
        return d
    if u.get("exclude_test_issues", True):
        d = d[~d["is_test"]]
    if u.get("exclude_etfs", True):
        d = d[~d["is_etf"]]
    bad_chars = u.get("exclude_symbol_chars", [])
    if bad_chars:
        d = d[~d["symbol"].apply(lambda s: any(c in s for c in bad_chars))]
    name_rx = u.get("exclude_name_regex")
    if name_rx:
        d = d[~d["name"].str.contains(name_rx, case=False, regex=True, na=False)]
    d = d.assign(in_directory=True)
    return d.reset_index(drop=True)


# ---------- liquidity filters (pure, testable) ----------

def apply_filters(candidates: pd.DataFrame, histories: dict[str, pd.DataFrame], cfg: Settings) -> pd.DataFrame:
    """Add last_close, avg_dollar_volume, bars, passed, reject_reasons to each candidate row."""
    f = cfg.section("filters")
    allowed = set(f["allowed_exchanges"])
    win = int(f["avg_dollar_volume_window"])
    rows = []
    for rec in candidates.to_dict("records"):
        sym = rec["symbol"]
        reasons: list[str] = []
        if not rec.get("in_directory", True):
            reasons.append("not in exchange listing (possible OTC/delisted)")
        elif rec.get("exchange") not in allowed:
            reasons.append(f"exchange {rec.get('exchange')} not allowed")
        df = histories.get(sym)
        last_close = adv = float("nan")
        bars = 0 if df is None else len(df)
        last_bar = None
        if df is None or df.empty:
            reasons.append("no price data")
        else:
            last_close = float(df["close"].iloc[-1])
            last_bar = df.index[-1].date()
            adv_series = ind.avg_dollar_volume(df, win)
            adv = float(adv_series.iloc[-1]) if adv_series.notna().any() else float("nan")
            if bars < int(f["min_history_days"]):
                reasons.append(f"only {bars} bars (< {f['min_history_days']})")
            if last_close < float(f["min_price"]):
                reasons.append(f"price {last_close:.2f} < {f['min_price']}")
            if pd.isna(adv):
                reasons.append(f"insufficient data for {win}d dollar volume")
            elif adv < float(f["min_avg_dollar_volume"]):
                reasons.append(f"{win}d avg $vol {adv/1e6:.1f}M < {float(f['min_avg_dollar_volume'])/1e6:.1f}M")
        rows.append({**rec, "last_close": last_close, "avg_dollar_volume": adv, "bars": bars,
                     "last_bar": last_bar, "passed": not reasons, "reject_reasons": reasons})
    return pd.DataFrame(rows)


# ---------- risk flags ----------

def reverse_split_flag(splits: pd.Series, lookback_days: int, today: date | None = None) -> str | None:
    """yfinance encodes a 1-for-10 reverse split as ratio 0.1. Returns a flag string or None."""
    if splits is None or splits.empty:
        return None
    cutoff = pd.Timestamp((today or date.today()) - timedelta(days=lookback_days))
    recent = splits[(splits.index >= cutoff) & (splits < 1)]
    if recent.empty:
        return None
    d, r = recent.index[-1].date(), recent.iloc[-1]
    return f"reverse split 1-for-{round(1 / r)} on {d}"


def attach_risk_flags(table: pd.DataFrame, price_provider, edgar_provider, cfg: Settings,
                      today: date | None = None) -> pd.DataFrame:
    """Add `risk_flags` (list[str]) for rows that passed. EDGAR is optional (None = skipped)."""
    rf = cfg.section("risk_flags")
    flags_col: list[list[str]] = []
    for rec in table.to_dict("records"):
        flags: list[str] = []
        if rec["passed"]:
            try:
                f = reverse_split_flag(price_provider.splits(rec["symbol"]),
                                       int(rf["reverse_split_lookback_days"]), today)
                if f:
                    flags.append(f)
            except Exception as e:  # data gap: say so, don't guess
                flags.append(f"split data unavailable ({type(e).__name__})")
            if edgar_provider is not None:
                try:
                    dil = edgar_provider.dilutive_filings(rec["symbol"], rf["dilutive_forms"],
                                                          int(rf["dilutive_filing_lookback_days"]), today)
                    for row in dil.head(3).itertuples():
                        flags.append(f"{row.form} filed {row.filing_date} ({row.url})")
                except Exception as e:
                    flags.append(f"EDGAR check unavailable ({type(e).__name__})")
        flags_col.append(flags)
    out = table.copy()
    out["risk_flags"] = flags_col
    if rf.get("reject_on_flag"):
        # Only real findings reject; "unavailable" notes are data gaps, not evidence.
        hard = out["risk_flags"].apply(lambda fl: [x for x in fl if "unavailable" not in x])
        hit = out["passed"] & hard.str.len().gt(0)
        out.loc[hit, "reject_reasons"] = hard[hit]
        out.loc[hit, "passed"] = False
    return out


# ---------- orchestration ----------

def build_universe(cfg: Settings | None = None, source: str | None = None, limit: int | None = None,
                   use_edgar: bool | None = None, price_provider=None, directory_provider=None,
                   edgar_provider=None, tickers: list[str] | None = None) -> UniverseResult:
    from core.providers.nasdaqtrader import NasdaqTraderProvider
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    source = source or cfg.get("universe.source")
    price_provider = price_provider or YFinanceProvider(cfg)
    directory_provider = directory_provider or NasdaqTraderProvider(cfg)

    directory = directory_provider.directory()
    # An explicit ticker list (e.g. names politicians bought) still goes through every filter.
    only = tickers if tickers is not None else (cfg.get("universe.tickers") if source == "list" else None)
    cands = select_candidates(directory, cfg, only=only)
    if limit:
        cands = cands.head(limit)
    log.info("%d candidates from %s", len(cands), source)

    tickers = cands.loc[cands["in_directory"], "symbol"].tolist()
    histories = price_provider.history(tickers, cfg.get("universe.history_period"))
    table = apply_filters(cands, histories, cfg)

    if use_edgar is None:
        use_edgar = bool(cfg.get("risk_flags.check_edgar_in_universe", False))
    if use_edgar and edgar_provider is None:
        from core.providers.edgar import EdgarProvider
        edgar_provider = EdgarProvider(cfg)
    table = attach_risk_flags(table, price_provider, edgar_provider if use_edgar else None, cfg)

    def _iso(ts):
        return ts.date().isoformat() if ts else None

    last_bar = table["last_bar"].dropna().max() if table["last_bar"].notna().any() else None
    sources = [
        {"name": "Nasdaq Trader symbol directory", "url": directory_provider.source_url,
         "as_of": _iso(directory_provider.last_fetched_at)},
        {"name": "Yahoo Finance daily prices (yfinance)", "url": price_provider.source_url,
         "as_of": last_bar.isoformat() if last_bar else None},
    ]
    if use_edgar:
        sources.append({"name": "SEC EDGAR filings", "url": "https://www.sec.gov/edgar/search/",
                        "as_of": date.today().isoformat()})
    return UniverseResult(table=table, sources=sources, histories=histories)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Build the filtered tradable universe.")
    ap.add_argument("--source", choices=["list", "nasdaqtrader"])
    ap.add_argument("--limit", type=int, help="only the first N candidates (for quick tests)")
    ap.add_argument("--edgar", action="store_true", help="also check EDGAR for dilutive filings")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = default_settings()
    res = build_universe(cfg, source=args.source, limit=args.limit, use_edgar=args.edgar or None)
    out_dir = cfg.path_for("paths.reports_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"universe-{date.today().isoformat()}.csv"
    t = res.table.copy()
    for col in ("reject_reasons", "risk_flags"):
        t[col] = t[col].apply(lambda xs: "; ".join(xs))
    t.to_csv(path, index=False)

    print(f"\nUniverse: {len(res.passed)} passed / {len(res.table)} candidates  ->  {path}")
    for s in res.sources:
        print(f"  source: {s['name']} ({s['url']}) as of {s['as_of'] or 'UNKNOWN'}")
    flagged = res.passed[res.passed["risk_flags"].str.len() > 0]
    if len(flagged):
        print(f"\n{len(flagged)} passed with risk flags:")
        for r in flagged.itertuples():
            print(f"  {r.symbol}: {'; '.join(r.risk_flags)}")
    rej = res.table[~res.table["passed"]]
    if len(rej):
        top = rej["reject_reasons"].explode().str.replace(r"[\d.]+", "#", regex=True).value_counts().head(8)
        print("\nTop rejection reasons:")
        for reason, n in top.items():
            print(f"  {n:>5}  {reason}")


if __name__ == "__main__":
    main()
