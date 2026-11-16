"""breakout-scanner: live scan.

    uv run python -m scanners.breakout.scan [--source list|nasdaqtrader] [--limit N] [--no-fundamentals]

Writes reports/breakout-YYYY-MM-DD.{json,md} (Signals) and breakout-YYYY-MM-DD-rejected.csv.
Research output only; no orders are placed.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timezone

import pandas as pd

from core import indicators as ind
from core.config import Settings, settings as default_settings
from core.fundamentals import FundamentalResult, evaluate_fundamentals
from core.signals import Confidence, KeyDate, PriceZone, Signal, Source, save_signals
from core.universe import build_universe
from scanners.breakout.setups import Plan, compute_features, make_plan, passes_gates, technical_score

log = logging.getLogger(__name__)
SKILL = "breakout-scanner"

def ordinal(n: float) -> str:
    n = int(round(n))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


SETUP_TEXT = {
    "breakout": "Broke out above its {L}-day pivot {pivot:.2f} today on {rvol:.1f}x volume",
    "pre_breakout": "Basing {dist:.1%} below its {L}-day pivot {pivot:.2f}",
    "pullback": "Pulled back to its rising {ma}",
}


def evaluate_last_bar(histories: dict[str, pd.DataFrame], bench_close: pd.Series, cfg: Settings):
    """Run gates + setups on the latest bar of every ticker. Returns (candidates, rejects)."""
    b = cfg.section("breakout")
    atr_p = int(cfg.get("indicators.atr_period"))
    rs_lb = int(cfg.get("indicators.rs_lookback_days"))
    rows = {}
    for t, df in histories.items():
        if len(df) < 200:
            continue
        rows[t] = compute_features(df, bench_close, b, atr_p, rs_lb).iloc[-1]
    if not rows:
        return [], []
    rs_pct = ind.percentile_rank(pd.Series({t: r["rs_ret"] for t, r in rows.items()}).dropna())

    candidates, rejects = [], []
    for t, row in rows.items():
        pct = float(rs_pct.get(t, float("nan")))
        if passes_gates(row, pct, b):
            continue                      # not a leader: not an idea, not worth listing
        plan, why = make_plan(row, b)
        if plan is None:
            if why:
                rejects.append({"ticker": t, "stage": "plan", "reason": why})
            continue
        candidates.append({"ticker": t, "row": row, "plan": plan, "rs_pct": pct,
                           "tech_score": technical_score(row, pct, b)})
    candidates.sort(key=lambda c: c["tech_score"], reverse=True)
    return candidates, rejects


def _confidence(fr: FundamentalResult | None, plan: Plan) -> Confidence:
    setup_hits = [h for h in plan.hits if h != "pullback"]
    if fr is None or fr.passed is None:
        return Confidence.low
    gaps = [n for n in fr.notes if "unavailable" in n or "unknown" in n]
    if fr.passed and not gaps and len(setup_hits) >= 2:
        return Confidence.high
    return Confidence.med


def build_signal(c: dict, fr: FundamentalResult | None, cfg: Settings, price_as_of: date,
                 fund_as_of: date | None, risk_flags: list[str], company: str) -> Signal:
    b = cfg.section("breakout")
    t, row, plan = c["ticker"], c["row"], c["plan"]
    L = int(b["base"]["length_days"])
    ma_label = {"ema21": "21-day EMA", "sma50": "50-day SMA"}.get(str(row["pullback_ma"]), "")
    setup_txt = SETUP_TEXT[plan.setup].format(
        L=L, pivot=plan.pivot or 0, rvol=row["rvol"], dist=plan.distance_to_pivot or 0, ma=ma_label)
    patterns = [h.replace("_", " ") for h in plan.hits if h not in ("breakout_day", "pullback")]
    thesis = (f"{setup_txt}" + (f" ({', '.join(patterns)})" if patterns else "") + ". "
              f"Leader: RS {ordinal(c['rs_pct'])} percentile vs SPY, 50>150>200 MA stack, "
              + (f"{-row['pct_from_high']:.0%} below its 52-week high." if row['pct_from_high'] < -0.005
                 else "at its 52-week high."))
    if fr is not None and fr.passed is not None:
        rv, ev = fr.metrics.get("revenue_yoy"), fr.metrics.get("eps_yoy")
        parts = [f"revenue {rv:+.0%}" if rv is not None else None, f"EPS {ev:+.0%}" if ev is not None else None]
        parts = [p for p in parts if p]
        if parts:
            thesis += f" Latest quarter YoY: {', '.join(parts)}."
    else:
        thesis += " Fundamentals could not be verified."

    score = c["tech_score"] + (b["scoring"]["fundamentals_pass"] if fr is not None and fr.passed else 0)
    flags = list(risk_flags)
    if fr is None or fr.passed is None:
        flags.append("fundamentals unverified")
    if fr is not None:
        flags += [n for n in fr.notes if "unavailable" in n]

    key_dates = []
    if fr is not None and fr.next_earnings:
        key_dates.append(KeyDate(date=fr.next_earnings, kind="earnings"))

    if plan.setup == "pre_breakout":
        inval = (f"Undercut of the base low {row['base_low']:.2f} / stop {plan.stop:.2f}, or no breakout "
                 f"through {plan.pivot:.2f} on volume within {b['backtest']['pending_days']} sessions.")
    elif plan.setup == "breakout":
        inval = f"Close back below the pivot {plan.pivot:.2f} on heavy volume, or a hit of the stop {plan.stop:.2f}."
    else:
        inval = f"Close below the {ma_label} / stop {plan.stop:.2f}; leaders shouldn't lose that average."

    yq = f"https://finance.yahoo.com/quote/{t}"
    sources = [Source(url=f"{yq}/history", as_of=price_as_of, note="daily OHLCV via yfinance")]
    if fr is not None and fund_as_of:
        sources.append(Source(url=f"{yq}/analysis", as_of=fund_as_of, note="EPS history, estimates, revisions"))
        sources.append(Source(url=f"{yq}/financials", as_of=fund_as_of, note="quarterly revenue"))

    metrics = {
        "setup": plan.setup,
        "price_as_of": price_as_of.isoformat(),
        # How the journal / backtest should fill this plan (see core.simulate.entry_mode)
        "entry_mode": "buy_stop" if plan.setup == "pre_breakout" else "next_open",
        "cancel_below": round(plan.cancel_below, 2) if plan.cancel_below else None,
        "pivot": round(plan.pivot, 2) if plan.pivot else None,
        "distance_to_pivot": round(plan.distance_to_pivot, 4) if plan.distance_to_pivot is not None else None,
        "rs_percentile": round(c["rs_pct"], 1),
        "rvol": round(float(row["rvol"]), 2) if pd.notna(row["rvol"]) else None,
        "base_depth": round(float(row["base_depth"]), 4),
        "atr": round(float(row["atr"]), 2),
        "target_basis": plan.target_basis,
        "technical_score": round(c["tech_score"], 1),
    }
    if fr is not None:
        metrics.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in fr.metrics.items()})

    return Signal(
        ticker=t, company=company or t, skill=SKILL, score=min(100.0, score), confidence=_confidence(fr, plan),
        thesis=thesis, signals_hit=plan.hits + (["ma_stack"] if row["ma_stack"] else []),
        entry_zone=PriceZone(low=round(plan.entry_low, 2), high=round(plan.entry_high, 2)),
        stop=round(plan.stop, 2), targets=[round(plan.target, 2)], horizon_days=int(b["plan"]["horizon_days"]),
        key_dates=key_dates, invalidation=inval, risk_flags=flags, sources=sources, metrics=metrics,
    )


def run(cfg: Settings | None = None, source: str | None = None, limit: int | None = None,
        use_fundamentals: bool = True, price_provider=None, today: date | None = None) -> dict:
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    b = cfg.section("breakout")
    today = today or date.today()
    price_provider = price_provider or YFinanceProvider(cfg)

    uni = build_universe(cfg, source=source, limit=limit, price_provider=price_provider)
    passed = uni.passed.set_index("symbol")
    histories = {t: uni.histories[t] for t in passed.index if t in uni.histories}
    bench_sym = cfg.get("indicators.benchmark")
    bench = price_provider.history([bench_sym], b["history_period"]).get(bench_sym)
    if bench is None or bench.empty:
        raise RuntimeError(f"benchmark {bench_sym} price data unavailable; RS cannot be computed")
    price_as_of = max(df.index[-1] for df in histories.values()).date()

    candidates, rejects = evaluate_last_bar(histories, bench["close"], cfg)
    log.info("%d leaders with a setup, %d rejected at plan stage", len(candidates), len(rejects))

    fb = b["fundamentals"]
    shortlist = candidates[: int(fb["shortlist_size"])]
    signals, deferred = [], []
    for c in shortlist:
        t = c["ticker"]
        fr, fund_as_of = None, None
        if use_fundamentals:
            raw = price_provider.fundamentals(t)
            fund_as_of = raw["fetched_at"].date()
            fr = evaluate_fundamentals(raw, today, float(fb["max_decel_pp"]), float(fb["revision_tolerance"]),
                                       float(fb["strong_growth_floor"]))
            d = fr.trading_days_to_earnings
            if d is not None and 0 <= d <= int(fb["earnings_exclusion_days"]):
                deferred.append({"ticker": t, "stage": "earnings",
                                 "reason": f"earnings {fr.next_earnings} in {d} trading days -> catalyst-plays"})
                continue
            if fr.passed is False:
                rejects.append({"ticker": t, "stage": "fundamentals", "reason": "; ".join(fr.fail_reasons)})
                continue
        rec = passed.loc[t]
        sig = build_signal(c, fr, cfg, price_as_of, fund_as_of, list(rec.get("risk_flags", []) or []),
                           str(rec.get("name", "") or ""))
        if sig.score < float(b["scoring"]["min_score"]):
            rejects.append({"ticker": t, "stage": "score", "reason": f"score {sig.score:.1f} < {b['scoring']['min_score']}"})
            continue
        signals.append(sig)
    for c in candidates[int(fb["shortlist_size"]):]:
        rejects.append({"ticker": c["ticker"], "stage": "shortlist",
                        "reason": f"technical score {c['tech_score']:.0f} below the top {fb['shortlist_size']} cut"})

    signals.sort(key=lambda s: s.score, reverse=True)
    signals = signals[: int(b["output_top_n"])]

    out_dir = cfg.path_for("paths.reports_dir")
    stem = f"breakout-{today.isoformat()}"
    json_path, md_path = save_signals(signals, out_dir, stem)
    rej_path = out_dir / f"{stem}-rejected.csv"
    pd.DataFrame(rejects + deferred, columns=["ticker", "stage", "reason"]).to_csv(rej_path, index=False)
    _write_header(md_path, signals, rejects, deferred, uni, price_as_of, use_fundamentals)
    return {"signals": signals, "rejects": rejects, "deferred": deferred, "universe": uni,
            "paths": {"json": json_path, "md": md_path, "rejected": rej_path}, "price_as_of": price_as_of}


def _write_header(md_path, signals, rejects, deferred, uni, price_as_of, use_fundamentals) -> None:
    body = md_path.read_text(encoding="utf-8")
    lines = [
        f"# Breakout scan — {date.today().isoformat()}",
        "",
        f"_Research only, not investment advice. Prices as of close {price_as_of}. "
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC._",
        "",
        f"- Universe: {len(uni.passed)} liquid stocks passed filters (of {len(uni.table)} candidates)",
        f"- Ideas: **{len(signals)}**; rejected after setup detection: {len(rejects)}; "
        f"deferred to catalyst-plays (earnings soon): {len(deferred)}",
    ]
    if not use_fundamentals:
        lines.append("- **Fundamentals were skipped** (--no-fundamentals): every idea is low confidence.")
    lines += ["- Sources: " + "; ".join(f"{s['name']} as of {s['as_of'] or 'UNKNOWN'}" for s in uni.sources), ""]
    if deferred:
        lines += ["## Deferred to catalyst-plays", ""] + [f"- {d['ticker']}: {d['reason']}" for d in deferred] + [""]
    lines += ["## Ideas", "", body]
    if rejects:
        lines += ["## Rejected", "", "| Ticker | Stage | Reason |", "|---|---|---|"]
        lines += [f"| {r['ticker']} | {r['stage']} | {r['reason']} |" for r in rejects]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Breakout scanner (research only).")
    ap.add_argument("--source", choices=["list", "nasdaqtrader"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-fundamentals", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    res = run(source=args.source, limit=args.limit, use_fundamentals=not args.no_fundamentals)
    print(f"\n{len(res['signals'])} ideas (prices as of {res['price_as_of']}) -> {res['paths']['md']}")
    for s in res["signals"]:
        m = s.metrics
        print(f"  {s.ticker:<6} {s.score:5.1f} {s.confidence.value:<4} {m['setup']:<12} "
              f"entry {s.entry_zone.low:.2f}-{s.entry_zone.high:.2f} stop {s.stop:.2f} "
              f"target {s.targets[0]:.2f} R:R {s.reward_risk:.1f}")
    if res["deferred"]:
        print("Deferred (earnings soon): " + ", ".join(d["ticker"] for d in res["deferred"]))
    print(f"Rejected: {len(res['rejects'])} (see {res['paths']['rejected']})")


if __name__ == "__main__":
    main()
