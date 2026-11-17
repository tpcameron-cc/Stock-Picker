"""momentum-hype: stocks whose attention is accelerating AND whose price confirms it. High risk by design.

    uv run python -m scanners.hype.scan

Flow: attention leaderboards (Reddit/4chan via ApeWisdom, r/wallstreetbets via Tradestie, StockTwits trending)
-> liquidity / safety universe filter -> price confirmation gate -> late-stage filter -> slow checks on a shortlist
(news headline velocity, call volume vs open interest, short interest) -> attention score, pump flags -> Signals.
Saves a daily attention snapshot (data/attention/) that builds the 30-day mention baseline over time.
Writes reports/hype-YYYY-MM-DD.{json,md} and -candidates.csv. Research only; no orders.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from core.config import Settings, settings as default_settings
from core.providers.attention import AttentionProvider, SnapshotStore, news_velocity
from core.providers.usaspending import core_name
from core.signals import Confidence, PriceZone, Signal, Source, lagging_data_flag, save_signals
from core.universe import build_universe
from scanners.hype.logic import attention_score, confirmation, late_stage, make_plan, pump_flags, squeeze_note

log = logging.getLogger(__name__)
SKILL = "momentum-hype"
NOT_COVERED = ["Google Trends: no official API (the unofficial client is routinely blocked).",
               "Borrow fee: no free source (iBorrowDesk unreachable).",
               "Reddit's own API needs a registered app; mention counts come from ApeWisdom / Tradestie aggregators."]


def _num(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)


def run(cfg: Settings | None = None, price_provider=None, attention=None, today: date | None = None) -> dict:
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    H = cfg.section("hype")
    prov = price_provider or YFinanceProvider(cfg)
    att = attention or AttentionProvider(cfg)
    today = today or date.today()
    store = SnapshotStore(cfg.path_for("hype.snapshot_dir"))

    # ---- attention leaderboards + today's snapshot ---------------------------------------------------------
    ape = att.apewisdom("all-stocks", int(H["apewisdom_pages"]))
    penny = att.apewisdom("pennystocks", 1)
    wsb = att.wsb()
    st = att.stocktwits_trending()
    store.save(today, {"apewisdom": ape.to_dict("records") if len(ape) else [],
                       "stocktwits": st.to_dict("records") if len(st) else [],
                       "wsb": wsb.to_dict("records") if len(wsb) else []})
    hist = store.mentions_history(today, int(H["baseline_days"]))
    hist_days = hist["date"].nunique() if len(hist) else 0
    baseline_mode = "snapshots" if hist_days >= int(H["min_baseline_days"]) else "yesterday"

    cands = set()
    if len(ape):
        cands |= set(ape.loc[ape["mentions"] >= int(H["min_mentions"]), "ticker"])
    cands |= set(st["ticker"]) if len(st) else set()
    cands |= set(wsb["ticker"].head(50)) if len(wsb) else set()
    cands = sorted(t for t in cands if t and t.replace("-", "").isalpha())
    log.info("%d attention candidates (baseline: %s, %d snapshot days)", len(cands), baseline_mode, hist_days)

    uni = build_universe(cfg, tickers=cands, price_provider=prov)
    tbl = uni.table.set_index("symbol")
    passed = uni.passed[~uni.passed["is_etf"].fillna(False).astype(bool)].set_index("symbol")
    price_as_of = max(uni.histories[t].index[-1] for t in passed.index if t in uni.histories).date() if len(passed) else today

    ape_i = ape.set_index("ticker") if len(ape) else pd.DataFrame()
    penny_i = penny.set_index("ticker") if len(penny) else pd.DataFrame()
    wsb_i = wsb.drop_duplicates("ticker").set_index("ticker") if len(wsb) else pd.DataFrame()
    st_i = st.drop_duplicates("ticker").set_index("ticker") if len(st) else pd.DataFrame()
    atr_p = int(cfg.get("indicators.atr_period"))

    rows, confirmed = [], []
    for t in cands:
        if t not in passed.index:
            why = "not exchange-listed / filtered"
            if t in tbl.index:
                why = "ETF" if bool(tbl.loc[t].get("is_etf")) else "; ".join(tbl.loc[t, "reject_reasons"]) or why
            rows.append({"ticker": t, "stage": "universe", "reason": why})
            continue
        df = uni.histories[t]
        m_now = _num(ape_i.loc[t, "mentions"]) if t in ape_i.index else None
        m_yday = _num(ape_i.loc[t, "mentions_24h_ago"]) if t in ape_i.index else None
        th = hist[hist["ticker"] == t]["mentions"] if len(hist) else pd.Series(dtype=float)
        if baseline_mode == "snapshots":
            base = float(th.mean()) if len(th) else 0.0         # absent from a day's leaderboard ~ few mentions
            base = base * len(th) / hist_days if hist_days else base
            peak = float(th.tail(7).max()) if len(th) else None
        else:
            base, peak = (m_yday or 0.0), None
        r_vel = (m_now / max(base, float(H["baseline_floor"]))) if m_now else None

        fails, conf = confirmation(df, H)
        late, ld = late_stage(df, m_now, peak, m_yday, H, atr_p)
        row = {"ticker": t, "company": passed.loc[t, "name"], "mentions": m_now, "mentions_baseline": round(base, 1),
               "baseline_mode": baseline_mode, "reddit_velocity": _r(r_vel), "st_rank": _num(st_i.loc[t, "rank"]) if t in st_i.index else None,
               "wsb_comments": _num(wsb_i.loc[t, "no_of_comments"]) if t in wsb_i.index else None,
               "wsb_sentiment": wsb_i.loc[t, "sentiment"] if t in wsb_i.index else None,
               **{k: _r(v) for k, v in conf.items()}, **{k: _r(v) for k, v in ld.items()}}
        if fails:
            rows.append({**row, "stage": "confirmation", "reason": "; ".join(fails)})
            continue
        if late:
            rows.append({**row, "stage": "late_stage", "reason": "; ".join(late)})
            continue
        confirmed.append(row)

    # ---- slow checks on the shortlist (by fast attention components) -------------------------------------------
    def fast_score(r):
        return attention_score(r["reddit_velocity"], None, r["st_rank"], r["wsb_comments"], r["wsb_sentiment"], None,
                               H["attention"])[0]
    confirmed.sort(key=fast_score, reverse=True)
    shortlist = confirmed[: int(H["max_shortlist"])]
    for r in confirmed[int(H["max_shortlist"]):]:
        rows.append({**r, "stage": "shortlist", "reason": f"attention below the top {H['max_shortlist']} confirmed names"})

    signals = []
    for r in shortlist:
        t = r["ticker"]
        news = att.news_counts(f'"{core_name(r["company"], 3)}" stock')
        n_vel, nd = news_velocity(news, price_as_of)
        calls = prov.call_activity(t)
        c_ratio = calls["call_volume"] / calls["call_oi"] if calls and calls["call_oi"] > 0 else None
        sq = prov.squeeze_context(t)
        score, parts = attention_score(r["reddit_velocity"], n_vel, r["st_rank"], r["wsb_comments"], r["wsb_sentiment"],
                                       c_ratio, H["attention"])
        pm = _num(penny_i.loc[t, "mentions"]) if t in penny_i.index else 0.0
        pflags = pump_flags(pm or 0.0, (sq or {}).get("market_cap"), list(tbl.loc[t, "risk_flags"] or []), H)
        score -= len(pflags) * float(H["pump"]["penalty"])
        row = {**r, **nd, "news_velocity": _r(n_vel), "call_vol_oi": _r(c_ratio),
               "call_put_ratio": _r(calls["call_volume"] / calls["put_volume"]) if calls and calls["put_volume"] else None,
               "short_pct_float": (sq or {}).get("short_pct_float"), "days_to_cover": (sq or {}).get("days_to_cover"),
               "market_cap": (sq or {}).get("market_cap"), "pump_flags": "; ".join(pflags), "score": round(score, 1),
               **{f"pts_{k}": round(v, 1) for k, v in parts.items()}}
        if len(pflags) >= int(H["pump"]["reject_on_flags"]):
            rows.append({**row, "stage": "pump", "reason": f"{len(pflags)} pump-and-dump warning signs"})
            continue
        if score < float(H["min_score"]):
            rows.append({**row, "stage": "score", "reason": f"attention score {score:.1f} < {H['min_score']}"})
            continue
        plan, why = make_plan(uni.histories[t], H, r["atr"])
        if plan is None:
            rows.append({**row, "stage": "plan", "reason": why})
            continue
        word = t in set(H.get("ambiguous_tickers") or [])
        weekend = baseline_mode == "yesterday" and (today.weekday() == 0 or today.weekday() >= 5)
        gaps = [g for g in [None if n_vel is not None else nd.get("news_note", "news velocity unavailable"),
                            "ticker is a common word: mention counts may be inflated" if word else None,
                            "baseline day was a weekend: mention velocity overstated" if weekend else None,
                            None if calls else "option activity unavailable",
                            "mention baseline = yesterday (snapshot history still building)" if baseline_mode == "yesterday" else None,
                            nd.get("news_note") if n_vel is not None else None] if g]
        bar_date = uni.histories[t].index[-1].date()
        gaps += [f for f in [lagging_data_flag(bar_date, price_as_of)] if f]
        signals.append(_signal(row, plan, pflags, squeeze_note(sq, H), gaps, bar_date))
        rows.append({**row, "stage": "idea", "reason": ""})

    signals.sort(key=lambda s: s.score, reverse=True)
    out = cfg.path_for("paths.reports_dir")
    stem = f"hype-{today.isoformat()}"
    jp, mp = save_signals(signals, out, stem)
    cand_df = pd.DataFrame(rows)
    cand_df.to_csv(out / f"{stem}-candidates.csv", index=False)
    _write_report(mp, signals, cand_df, cands, price_as_of, baseline_mode, hist_days, att.errors, H)
    return {"signals": signals, "candidates": cand_df, "price_as_of": price_as_of, "baseline_mode": baseline_mode,
            "snapshot_days": hist_days, "errors": att.errors, "paths": {"json": jp, "md": mp}}


def _r(x, nd=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def _signal(r: dict, plan, pflags: list[str], sq_note: str | None, gaps: list[str], price_as_of: date) -> Signal:
    t = r["ticker"]
    src = []
    if r.get("mentions") is not None:
        src.append(f"{r['mentions']:.0f} Reddit/4chan mentions in 24h ({r['reddit_velocity']:.1f}x baseline)")
    if r.get("news_velocity") is not None:
        src.append(f"news headlines {r['news_velocity']:.1f}x their daily average")
    if r.get("st_rank") is not None:
        src.append(f"StockTwits trending #{r['st_rank']:.0f}")
    if r.get("call_vol_oi") is not None:
        src.append(f"call volume {r['call_vol_oi']:.0%} of call open interest")
    thesis = (f"Attention accelerating: {', '.join(src) or 'attention data limited'}. Price confirms: closed above rising "
              f"10/20-day averages on {r['rvol']:.1f}x volume, {r['day_change']:+.1%} on the day, at {r['close_in_range']:.0%} "
              f"of the day's range. HIGH RISK momentum trade with a tight stop and a short time stop.")
    hits = ["attention_velocity", "price_confirmed"] + (["stocktwits_trending"] if r.get("st_rank") else []) + \
           (["unusual_calls"] if (r.get("call_vol_oi") or 0) >= 0.5 else []) + (["squeeze_setup"] if sq_note and sq_note.startswith("squeeze") else [])
    flags = ["HIGH RISK: hype / momentum trade"] + plan.flags + pflags + ([sq_note] if sq_note else []) + gaps
    conf = Confidence.low if pflags or len(gaps) >= 2 else Confidence.med        # never high for hype
    yq = f"https://finance.yahoo.com/quote/{t}"
    sources = [Source(url="https://apewisdom.io/", as_of=date.today(), note="Reddit/4chan mention counts"),
               Source(url=f"{yq}/history", as_of=price_as_of, note="prices")]
    if r.get("st_rank") is not None:
        sources.append(Source(url=f"https://stocktwits.com/symbol/{t}", as_of=date.today(), note="StockTwits trending"))
    if r.get("news_velocity") is not None:
        sources.append(Source(url="https://news.google.com/", as_of=date.today(), note="headline counts"))
    if r.get("call_vol_oi") is not None or r.get("short_pct_float") is not None:
        sources.append(Source(url=f"{yq}/options", as_of=date.today(), note="options activity / short interest"))
    metrics = {"setup": "momentum_hype", "price_as_of": price_as_of.isoformat(), "entry_mode": "next_open",
               "stop_basis": plan.stop_basis, "mentions": r.get("mentions"), "mentions_baseline": r.get("mentions_baseline"),
               "baseline_mode": r.get("baseline_mode"), "reddit_velocity": r.get("reddit_velocity"),
               "news_velocity": r.get("news_velocity"), "st_rank": r.get("st_rank"), "wsb_comments": r.get("wsb_comments"),
               "call_vol_oi": r.get("call_vol_oi"), "rvol": r.get("rvol"), "atrs_above_sma20": r.get("atrs_above_sma20"),
               "gain_5d": r.get("gain_5d"), "short_pct_float": r.get("short_pct_float"), "days_to_cover": r.get("days_to_cover"),
               **{k: v for k, v in r.items() if k.startswith("pts_")}}
    return Signal(ticker=t, company=r["company"], skill=SKILL, score=float(np.clip(r["score"], 0, 100)), confidence=conf,
                  thesis=thesis, signals_hit=hits, entry_zone=PriceZone(low=round(plan.entry_low, 2), high=round(plan.entry_high, 2)),
                  stop=round(plan.stop, 2), targets=[round(x, 2) for x in plan.targets], horizon_days=plan.horizon_days,
                  invalidation=f"Close below {plan.stop:.2f}, a close back under the 10-day average, or no follow-through "
                               f"within {plan.horizon_days} sessions.",
                  risk_flags=flags, sources=sources, metrics=metrics)


def _write_report(md_path, signals, cand: pd.DataFrame, cands, price_as_of, baseline_mode, hist_days, errors, H):
    body = md_path.read_text(encoding="utf-8")
    stages = cand["stage"].value_counts().to_dict() if len(cand) else {}
    L = [f"# Momentum / hype — {date.today().isoformat()}", "",
         f"_HIGH RISK. Research only, not investment advice. Prices as of close {price_as_of}. "
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC._", "",
         f"- Attention candidates: {len(cands)} (Reddit/4chan leaderboard, StockTwits trending, r/wallstreetbets)",
         f"- Funnel: " + ", ".join(f"{k} {v}" for k, v in stages.items()),
         f"- Mention baseline: **{baseline_mode}** ({hist_days} snapshot days saved; true 30-day baseline after "
         f"{H['min_baseline_days']}+ days of daily runs)",
         f"- Data errors: {', '.join(errors) or 'none'}", "", "## Ideas", "", body]
    watch = cand[cand["stage"] == "confirmation"].dropna(subset=["reddit_velocity"]).sort_values("reddit_velocity", ascending=False).head(10) \
        if len(cand) and "reddit_velocity" in cand else pd.DataFrame()
    if len(watch):
        L += ["## Hype without price confirmation (watch, don't chase)", "", "| Ticker | Mentions | Velocity | Why not |", "|---|---|---|---|"]
        L += [f"| {r.ticker} | {r.mentions:.0f} | {r.reddit_velocity:.1f}x | {r.reason} |" for r in watch.itertuples()]
        L.append("")
    late = cand[cand["stage"].isin(["late_stage", "pump"])] if len(cand) else pd.DataFrame()
    if len(late):
        L += ["## Rejected as late-stage or pump-like", "", "| Ticker | Stage | Reason |", "|---|---|---|"]
        L += [f"| {r.ticker} | {r.stage} | {r.reason} |" for r in late.itertuples()]
        L.append("")
    L += ["## Not covered", ""] + [f"- {x}" for x in NOT_COVERED]
    md_path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description="Momentum / hype scan (research only, high risk).").parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    res = run()
    print(f"\n{len(res['signals'])} ideas (prices as of {res['price_as_of']}; baseline {res['baseline_mode']}, "
          f"{res['snapshot_days']} snapshot days) -> {res['paths']['md']}")
    for s in res["signals"]:
        m = s.metrics
        print(f"  {s.ticker:<6} {s.score:5.1f} {s.confidence.value:<4} mentions {m['mentions'] or 0:>5.0f} "
              f"vel {m['reddit_velocity'] or 0:.1f}x rvol {m['rvol']:.1f} entry {s.entry_zone.low:.2f}-{s.entry_zone.high:.2f} "
              f"stop {s.stop:.2f} T {', '.join(f'{x:.2f}' for x in s.targets)}")
    if not res["candidates"].empty:
        print(res["candidates"]["stage"].value_counts().to_string())
    if res["errors"]:
        print("Data errors:", res["errors"])


if __name__ == "__main__":
    main()
