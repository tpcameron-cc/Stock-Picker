"""political-edge: purchases by well-performing members of Congress in companies their committees oversee.

    uv run python -m scanners.political.scan

Data: House Periodic Transaction Reports parsed from the Clerk's PDFs (Senate not yet covered), committee
assignments (congress-legislators), a curated committee->agency/industry map, USAspending contracts, Yahoo prices
and profiles, SEC Form 4 (if SEC_USER_AGENT is set). Writes reports/political-YYYY-MM-DD.{json,md} plus
-members.csv (Step A screen) and -purchases.csv (Step C classification). Research only; no orders.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core import indicators as ind
from core.config import Settings, settings as default_settings
from core.providers.congress import match_member
from core.signals import Confidence, PriceZone, Signal, Source, save_signals
from core.universe import build_universe
from scanners.political.oversight import classify, load_jurisdiction, member_oversight
from scanners.political.score import score_purchase
from scanners.political.screen import amount_mid, apply_criteria, member_stats, price_on_or_after

log = logging.getLogger(__name__)
SKILL = "political-edge"
NOT_COVERED = [
    "Senate trades: the Senate eFD site requires accepting its terms before searching; not done without your OK.",
    "Lobbying (Senate LDA), campaign money (FEC), committee testimony, White House visitor logs: not wired in yet.",
    "Bills sponsored / hearing, markup and vote dates: need a congress.gov API key (free from api.data.gov).",
    "Trump administration holdings, Trump mentions and Trump sponsor ties: only available via InsiderCat (no active plan).",
    "Members leaving office in January: no structured source for retirements / election losses; not flagged.",
    "Scanned (paper) PTRs have no text layer and are counted as unparsed, not read.",
]


def _s(x) -> str:
    """Text field that may be None/NaN -> '' (NaN is truthy, so never test it directly)."""
    return "" if x is None or (isinstance(x, float) and np.isnan(x)) else str(x)


def _r(x, nd=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def run(cfg: Settings | None = None, price_provider=None, house=None, congress=None, contracts_provider=None,
        edgar=None, today: date | None = None) -> dict:
    from core.providers.congress import CongressProvider
    from core.providers.house_disclosures import HouseDisclosureProvider
    from core.providers.usaspending import USAspendingProvider
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    P = cfg.section("political")
    S, PU, SC, PL = P["screen"], P["purchases"], P["scoring"], P["plan"]
    prov = price_provider or YFinanceProvider(cfg)
    house = house or HouseDisclosureProvider(cfg)
    congress = congress or CongressProvider(cfg)
    contracts_provider = contracts_provider or USAspendingProvider(cfg)
    if edgar is None and cfg.env("providers.edgar.user_agent_env"):
        from core.providers.edgar import EdgarProvider
        edgar = EdgarProvider(cfg)
    today = today or date.today()

    # ---- ingest -------------------------------------------------------------------------------------------
    years = [today.year - k for k in range(int(P["years_back"]))]
    tx, coverage = house.transactions(years, progress=True)
    members = congress.members()
    assignments = congress.committee_assignments()
    juris = load_jurisdiction(cfg.path_for("political.jurisdiction_file"))
    keys = tx[["last", "state_district"]].drop_duplicates()
    bmap = {(r.last, r.state_district): match_member(members, r.last, r.state_district) for r in keys.itertuples()}
    tx["bioguide"] = [bmap.get((a, b)) for a, b in zip(tx["last"], tx["state_district"])]
    unmatched = sorted({f"{a} ({b})" for (a, b), v in bmap.items() if v is None})
    tx = tx[tx["trade_date"].notna() & tx["type"].isin(["P", "S", "E"])]

    long_window = int(P["years_back"]) >= 4
    period = "5y" if long_window else "2y"
    st_tickers = sorted(set(tx.loc[(tx["asset_type"] == "ST") & tx["ticker"].notna(), "ticker"]))
    bench = cfg.get("indicators.benchmark")
    hist = prov.history(st_tickers + [bench], period)
    if bench not in hist:
        raise RuntimeError("benchmark prices unavailable")
    price_as_of = hist[bench].index[-1].date()
    closes = {t: df["close"] for t, df in hist.items()}
    spy = closes[bench]

    # ---- Step A: screen members ---------------------------------------------------------------------------
    names = members.set_index("bioguide")
    rows = []
    for bg, g in tx[tx["bioguide"].notna()].groupby("bioguide"):
        st = member_stats(g, closes, spy, price_as_of, S, long_window)
        if st["n_trades"] == 0:
            continue
        ok, fails, flags, exception = apply_criteria(st, S)
        m = names.loc[bg]
        rows.append({"bioguide": bg, "member": m["full_name"], "party": m["party"], "seat": m["state_district"],
                     "qualified": ok, "exception": exception, "fail_reasons": "; ".join(fails),
                     "flags": "; ".join(flags), **{k: v for k, v in st.items()}})
    screen = pd.DataFrame(rows).sort_values(["qualified", "excess_1y"], ascending=[False, False])
    qualified = screen[screen["qualified"]]
    log.info("%d members with trades, %d qualify", len(screen), len(qualified))

    ref_notes = []
    for ref in S.get("reference_exceptions", []):
        hit = screen[screen["member"].str.contains(ref, case=False, na=False)]
        if len(hit):
            r = hit.iloc[0]
            ref_notes.append(f"{r['member']}: {'qualifies' if r['qualified'] else 'does not qualify'}"
                             + (f" ({_s(r['exception'])})" if _s(r["exception"]) else "")
                             + (f" — {_s(r['fail_reasons'])}" if _s(r["fail_reasons"]) else ""))
        else:
            senate = members[(members["full_name"].str.contains(ref, case=False)) & (members["chamber"] == "senate")]
            ref_notes.append(f"{ref}: " + ("Senate member — Senate trades not covered yet" if len(senate)
                                           else "no parsed House PTR trades in the window"))

    # ---- Step C: classify qualifying members' recent purchases ---------------------------------------------
    since = price_as_of - timedelta(days=int(PU["lookback_days"]))
    buys = tx[(tx["bioguide"].isin(qualified["bioguide"])) & (tx["type"] == "P") & (tx["asset_type"] == "ST")
              & tx["ticker"].notna() & (tx["trade_date"] >= since)].copy()
    uni = build_universe(cfg, tickers=sorted(set(buys["ticker"])), price_provider=prov) if len(buys) else None
    passed = uni.passed.set_index("symbol") if uni is not None else pd.DataFrame()
    purchases, signals, rejects = [], [], []
    c_start = today - timedelta(days=int(PU["contracts_lookback_days"]))
    for r in buys.itertuples():
        if r.ticker not in passed.index:
            why = "not in liquid universe"
            if uni is not None and r.ticker in set(uni.table["symbol"]):
                why = "; ".join(uni.table.set_index("symbol").loc[r.ticker, "reject_reasons"])
            purchases.append({"ticker": r.ticker, "member": names.loc[r.bioguide, "full_name"], "trade_date": r.trade_date,
                              "link": "—", "note": f"filtered: {why}"})
            continue
        company = str(passed.loc[r.ticker, "name"])
        prof = prov.profile(r.ticker) or {}
        try:
            contracts = contracts_provider.contracts(company, c_start, today)
        except Exception as e:
            log.info("contracts unavailable for %s: %s", r.ticker, e)
            contracts = None
        ov = member_oversight(r.bioguide, assignments, juris)
        cl = classify(ov, prof.get("sector"), prof.get("industry"), contracts, r.trade_date, PU)
        others = buys[(buys["ticker"] == r.ticker) & (buys["bioguide"] != r.bioguide)]["bioguide"].nunique()
        insider = None
        if edgar is not None:
            try:
                insider = edgar.insider_open_market_buys(r.ticker, int(PU["lookback_days"]), today)
            except Exception as e:
                cl["gaps"].append(f"insider Form 4 check failed ({type(e).__name__})")
        else:
            cl["gaps"].append("insider Form 4 cluster not checked (set SEC_USER_AGENT)")
        df = hist.get(r.ticker)
        if df is None:
            df = prov.history([r.ticker], period).get(r.ticker)
        px_disc = price_on_or_after(df["close"], r.filing_date) if df is not None and r.filing_date else None
        close = float(df["close"].iloc[-1]) if df is not None else None
        move = close / px_disc - 1 if px_disc and close else None
        delay = (r.filing_date - r.trade_date).days if r.filing_date else 0
        mid = amount_mid(r.amount_low, r.amount_high) or 0
        score, parts = score_purchase(cl["link"], others, mid, (price_as_of - r.trade_date).days, delay, move,
                                      insider is not None and len(insider) > 0, SC, int(PU["lookback_days"]),
                                      int(S["max_median_filing_delay"]))
        mrow = qualified.set_index("bioguide").loc[r.bioguide]
        purchases.append({
            "ticker": r.ticker, "company": company, "member": mrow["member"], "party": mrow["party"],
            "seat": mrow["seat"], "trade_date": r.trade_date, "filing_date": r.filing_date, "owner": r.owner,
            "amount": f"${r.amount_low:,.0f}–" + (f"${r.amount_high:,.0f}" if pd.notna(r.amount_high) else "over"),
            "account": _s(getattr(r, "account", None)) or None,
            "link": cl["link"], "evidence": "; ".join(cl["evidence"]), "timing": "; ".join(cl["timing_flags"]),
            "link_flags": "; ".join(cl["flags"]), "committee_contract_share": _r(cl["committee_contract_share"]),
            "gaps": "; ".join(cl["gaps"]), "other_qualified_buyers": others,
            "insider_buys": None if insider is None else len(insider), "move_since_disclosure": _r(move),
            "filing_delay_days": delay, "score": round(score, 1), "sector": prof.get("sector"),
            "industry": prof.get("industry"), "committees": "; ".join(ov.committees), "leadership": "; ".join(ov.leadership),
            "member_flags": mrow["flags"], "member_exception": mrow["exception"], "source_url": r.source_url,
            "bioguide": r.bioguide, "_parts": parts, "_df": df, "_mid": mid})

    # One idea per ticker: the strongest purchase, with every qualifying buyer listed.
    pdf = pd.DataFrame(purchases)
    if len(pdf) and "score" in pdf:
        cand = pdf[pdf["score"].notna()]
        for tk, g in cand.groupby("ticker"):
            best = g.sort_values("score", ascending=False).iloc[0]
            if PU["require_overlap"] and best["link"] not in ("direct", "sector"):
                rejects.append({"ticker": tk, "reason": "no committee overlap (NONE)"})
                continue
            if best["score"] < float(SC["min_score"]):
                rejects.append({"ticker": tk, "reason": f"score {best['score']:.1f} < {SC['min_score']}"})
                continue
            sig, why = _build_signal(best, g, cfg, price_as_of, congress)
            if sig is None:
                rejects.append({"ticker": tk, "reason": why})
            else:
                signals.append(sig)
    signals.sort(key=lambda s: s.score, reverse=True)

    out = cfg.path_for("paths.reports_dir")
    stem = f"political-{today.isoformat()}"
    jp, mp = save_signals(signals, out, stem)
    screen.to_csv(out / f"{stem}-members.csv", index=False)
    pcols = [c for c in pdf.columns if not c.startswith("_")] if len(pdf) else []
    pdf[pcols].to_csv(out / f"{stem}-purchases.csv", index=False) if len(pdf) else None
    _write_report(mp, signals, screen, pdf, rejects, coverage, unmatched, ref_notes, price_as_of, years, cfg, edgar)
    return {"signals": signals, "screen": screen, "purchases": pdf, "rejects": rejects, "coverage": coverage,
            "unmatched": unmatched, "ref_notes": ref_notes, "price_as_of": price_as_of, "paths": {"json": jp, "md": mp}}


def _build_signal(best: pd.Series, group: pd.DataFrame, cfg: Settings, price_as_of: date, congress
                  ) -> tuple[Signal | None, str | None]:
    PL = cfg.section("political.plan")
    df = best["_df"]
    close = float(df["close"].iloc[-1])
    atr = float(ind.atr(df, int(cfg.get("indicators.atr_period"))).iloc[-1])
    z = float(PL["entry_zone_pct"])
    lo, hi = close * (1 - z), close * (1 + z)
    stop = close - float(PL["atr_stop_mult"]) * atr
    if (lo - stop) / lo > float(PL["max_stop_pct"]):
        return None, f"ATR stop {(lo - stop) / lo:.1%} below entry (> {float(PL['max_stop_pct']):.0%})"
    if stop <= 0 or stop >= lo:
        return None, "no valid stop"
    mid = (lo + hi) / 2
    target = mid + float(PL["target_r"]) * (mid - stop)

    buyers = sorted(set(group["member"]))
    link = best["link"]
    thesis = (f"{best['member']} ({best['party']}, {best['seat']}) bought {best['amount']} on {best['trade_date']} "
              f"(filed {best['filing_date']}); {link.upper()} committee link: {best['evidence']}.")
    if len(buyers) > 1:
        thesis += f" Other qualifying buyers: {', '.join(b for b in buyers if b != best['member'])}."
    thesis += " Member qualified on copy-trade performance vs SPY (House PTRs)."
    exception, mflags, gaps = _s(best["member_exception"]), _s(best["member_flags"]), _s(best["gaps"])
    flags = [f for f in [_s(best["timing"]), _s(best["link_flags"]), mflags, exception, gaps] if f]
    flags.append("disclosure signal: public PTR data, possibly weeks old")
    hits = [f"{link}_overlap"] + (["timing_flag"] if _s(best["timing"]) else []) + \
           (["agreement"] if best["other_qualified_buyers"] else []) + (["insider_cluster"] if best["insider_buys"] else [])
    conf = (Confidence.high if link == "direct" and not exception and "borderline" not in mflags
            and "unavailable" not in gaps else Confidence.med if link == "direct" or not exception
            else Confidence.low)
    sources = [Source(url=u, as_of=d, note="House PTR") for u, d in
               group[["source_url", "filing_date"]].drop_duplicates().head(4).itertuples(index=False)]
    sources += [Source(url="https://github.com/unitedstates/congress-legislators", as_of=price_as_of,
                       note="committee assignments"),
                Source(url=f"https://finance.yahoo.com/quote/{best['ticker']}/history", as_of=price_as_of, note="prices")]
    if link == "direct":
        sources.append(Source(url="https://www.usaspending.gov/search", as_of=date.today(), note="federal contracts"))
    metrics = {"setup": "political_purchase", "price_as_of": price_as_of.isoformat(), "entry_mode": "next_open",
               "link": link, "member": best["member"], "bioguide": best["bioguide"], "buyers": ", ".join(buyers),
               "trade_date": str(best["trade_date"]), "filing_date": str(best["filing_date"]),
               "amount": best["amount"], "filing_delay_days": int(best["filing_delay_days"]),
               "move_since_disclosure": best["move_since_disclosure"], "leadership": _s(best["leadership"]) or None,
               "target_basis": f"{float(PL['target_r']):g}R (no measured target for a disclosure signal)",
               **{f"pts_{k}": round(v, 1) for k, v in best["_parts"].items()}}
    sig = Signal(ticker=best["ticker"], company=best["company"], skill=SKILL, score=float(np.clip(best["score"], 0, 100)),
                 confidence=conf, thesis=thesis, signals_hit=hits,
                 entry_zone=PriceZone(low=round(lo, 2), high=round(hi, 2)), stop=round(stop, 2),
                 targets=[round(target, 2)], horizon_days=int(PL["horizon_days"]),
                 invalidation=f"Close below {stop:.2f}, or the member discloses a sale.",
                 risk_flags=flags, sources=sources, metrics=metrics)
    return sig, None


def _write_report(md_path, signals, screen, pdf, rejects, coverage, unmatched, ref_notes, price_as_of, years, cfg, edgar):
    body = md_path.read_text(encoding="utf-8")
    S = cfg.section("political.screen")
    cov = coverage
    scanned = int((~cov["parsed"] & cov["note"].str.contains("scanned", na=False)).sum()) if len(cov) else 0
    L = [f"# Political edge — {date.today().isoformat()}", "",
         f"_Research only, not investment advice. Public disclosures only. Prices as of close {price_as_of}. "
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC._", "",
         f"- House PTRs {', '.join(map(str, years))}: {len(cov)} filings, {int(cov['parsed'].sum())} parsed, "
         f"{scanned} scanned (unreadable), {len(cov) - int(cov['parsed'].sum()) - scanned} other unparsed",
         f"- Members with trades: {len(screen)}; qualified: **{int(screen['qualified'].sum())}**; "
         f"unmatched filers: {', '.join(unmatched) or 'none'}",
         f"- Ideas: **{len(signals)}**; purchases classified: {len(pdf)}; rejected names: {len(rejects)}",
         "- Performance is copy-trade: buys at amount-range midpoints, priced at the trade date (or filing date for "
         "delay-adjusted), sells FIFO, vs SPY with identical cash flows. Portfolio size = PTR-traded book, not net worth.",
         "", "## Reference exceptions", ""] + [f"- {n}" for n in ref_notes] + [""]
    L += ["## Ideas", "", body]
    q = screen[screen["qualified"]]
    L += ["## Step A — qualifying members", "",
          "| Member | Party | Seat | Trades | 1Y copy-trade | vs SPY | Delay-adj. vs SPY | Ex-best vs SPY | Med. delay | Est. book | Flags / exception |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in q.itertuples():
        L.append(f"| {r.member} | {r.party} | {r.seat} | {r.n_trades} | {r.ret_1y:+.1%} | {r.excess_1y:+.1%} | "
                 f"{r.excess_1y_delay:+.1%} | {r.excess_ex_best:+.1%} | {r.median_filing_delay:.0f}d | "
                 f"${r.est_portfolio:,.0f} | {'; '.join(x for x in [_s(r.flags), _s(r.exception)] if x) or '—'} |")
    near = screen[~screen["qualified"] & (screen["n_trades"] >= int(S["min_trades"]))].head(15)
    if len(near):
        L += ["", "Active traders that did not qualify (top 15):", "", "| Member | Trades | vs SPY | Why not |", "|---|---|---|---|"]
        for r in near.itertuples():
            ex = f"{r.excess_1y:+.1%}" if pd.notna(r.excess_1y) else "—"
            L.append(f"| {r.member} | {r.n_trades} | {ex} | {_s(r.fail_reasons)} |")
    if len(pdf):
        L += ["", "## Step C — recent purchases by qualifying members", "",
              "| Ticker | Member | Traded | Filed | Amount | Link | Evidence | Timing | Score |", "|---|---|---|---|---|---|---|---|---|"]
        for r in pdf.sort_values("score", ascending=False, na_position="last").itertuples():
            L.append(f"| {r.ticker} | {r.member} | {r.trade_date} | {_s(getattr(r, 'filing_date', None))} | "
                     f"{_s(getattr(r, 'amount', None))} | "
                     f"{r.link} | {_s(getattr(r, 'evidence', None)) or _s(getattr(r, 'note', None))} | "
                     f"{_s(getattr(r, 'timing', None)) or '—'} | "
                     f"{'' if pd.isna(getattr(r, 'score', np.nan)) else f'{r.score:.0f}'} |")
    if rejects:
        L += ["", "## Rejected", ""] + [f"- {x['ticker']}: {x['reason']}" for x in rejects]
    L += ["", "## Not covered yet", ""] + [f"- {x}" for x in NOT_COVERED]
    if edgar is None:
        L.append("- Insider Form 4 cluster buying: off until SEC_USER_AGENT is set.")
    L += ["", "_Committee→industry links use a curated map of House Rule X / Senate Rule XXV jurisdiction "
              "(config/committee_jurisdiction.yaml). Contract links match companies to USAspending recipients by name._"]
    md_path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Political edge (research only).")
    ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    res = run()
    sc = res["screen"]
    print(f"\nMembers with trades: {len(sc)}; qualified: {int(sc['qualified'].sum())}")
    for n in res["ref_notes"]:
        print("  ref:", n)
    print(f"{len(res['signals'])} ideas (prices as of {res['price_as_of']}) -> {res['paths']['md']}")
    for s in res["signals"]:
        m = s.metrics
        print(f"  {s.ticker:<6} {s.score:5.1f} {s.confidence.value:<4} {m['link']:<6} {m['member'][:28]:<28} "
              f"traded {m['trade_date']} entry {s.entry_zone.low:.2f}-{s.entry_zone.high:.2f} stop {s.stop:.2f}")


if __name__ == "__main__":
    main()
