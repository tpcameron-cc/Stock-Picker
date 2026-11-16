"""catalyst-plays: earnings plays for the next 3 weeks + post-earnings drift entries.

    uv run python -m scanners.catalyst.scan [--source list|nasdaqtrader] [--limit N]

Writes reports/catalyst-YYYY-MM-DD.{json,md} (Signals for actionable plays) and -watch.csv / -rejected.csv.
Research output only; no orders are placed.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core import indicators as ind
from core.config import Settings, settings as default_settings
from core.signals import Confidence, KeyDate, PriceZone, Signal, Source, save_signals
from core.universe import build_universe
from scanners.catalyst.history import event_stats, history_table, reaction_index, summarize_history
from scanners.catalyst.plays import (
    CatalystPlan, choose_play, evaluate_drift, event_implied_move, guidance_proxy, implied_ratio, last_exit_day, lockup_flag,
    plan_upcoming, revision_state, score_upcoming, sessions_between,
)

log = logging.getLogger(__name__)
SKILL = "catalyst-plays"
NOT_COVERED = [
    "FDA PDUFA / advisory-committee dates: no free machine-readable source (FDA calendar renders via JavaScript; "
    "PDUFA dates are company-disclosed only).",
    "Index additions, investor days, product launches: no free structured feed.",
    "Large federal contract awards: built with political-edge (stage 5), which links them to committee members.",
]


def _reaction_day(report: date, timing: str) -> date:
    d = pd.Timestamp(report)
    if timing == "bmo":
        return (d if d.weekday() < 5 else d + pd.offsets.BDay(1)).date()
    return (d + pd.offsets.BDay(1)).date()


def _negative_flags(t: str, prov, edgar, cfg: Settings, today: date) -> list[str]:
    c = cfg.section("catalyst")
    flags = []
    prof = prov.profile(t)
    lf = lockup_flag(prof.get("first_trade_date") if prof else None, today, c)
    if lf:
        flags.append(lf)
    if edgar is not None:
        try:
            dil = edgar.dilutive_filings(t, cfg.get("risk_flags.dilutive_forms"),
                                         int(c["negative"]["offering_lookback_days"]), today)
            for row in dil.head(2).itertuples():
                flags.append(f"recent offering filing {row.form} on {row.filing_date} ({row.url})")
        except Exception as e:
            flags.append(f"offering check unavailable ({type(e).__name__})")
    return flags


def _sources(t: str, price_as_of: date, cal_as_of: date, implied: dict | None) -> list[Source]:
    yq = f"https://finance.yahoo.com/quote/{t}"
    src = [Source(url=f"{yq}/history", as_of=price_as_of, note="daily OHLCV via yfinance"),
           Source(url="https://www.nasdaq.com/market-activity/earnings", as_of=cal_as_of, note="earnings calendar"),
           Source(url=f"{yq}/analysis", as_of=cal_as_of, note="EPS history, surprises, estimate trend")]
    if implied:
        src.append(Source(url=f"{yq}/options", as_of=cal_as_of, note=f"ATM straddle, expiry {implied['expiry']}"))
    return src


def _signal(t: str, company: str, plan: CatalystPlan, score: float, conf: Confidence, thesis: str, hits: list[str],
            key_dates: list[KeyDate], invalidation: str, flags: list[str], sources: list[Source], metrics: dict,
            price_as_of: date) -> Signal:
    return Signal(
        ticker=t, company=company or t, skill=SKILL, score=float(np.clip(score, 0, 100)), confidence=conf,
        thesis=thesis, signals_hit=hits,
        entry_zone=PriceZone(low=round(plan.entry_low, 2), high=round(plan.entry_high, 2)),
        stop=round(plan.stop, 2), targets=[round(plan.target, 2)], horizon_days=max(1, int(plan.horizon_days)),
        key_dates=key_dates, invalidation=invalidation, risk_flags=plan.flags + flags, sources=sources,
        metrics={"setup": plan.setup, "play": plan.play, "price_as_of": price_as_of.isoformat(),
                 "entry_mode": plan.entry_mode, "target_basis": plan.target_basis, **metrics},
    )


def _r(x, nd=4):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def analyze_upcoming(rows: pd.DataFrame, hist: dict, prov, edgar, cfg: Settings, price_as_of: date,
                     names: dict) -> tuple[list[Signal], list[dict], list[dict]]:
    c = cfg.section("catalyst")
    signals, watch, rejects = [], [], []
    for row in rows.itertuples():
        t = row.symbol
        df = hist.get(t)
        if df is None or len(df) < 60:
            rejects.append({"ticker": t, "list": "upcoming", "reason": "insufficient price history"})
            continue
        eh = prov.earnings_history(t)
        timing = row.timing
        if timing == "unknown" and not eh.empty:
            same = eh[eh["date"] == row.date]
            if len(same):
                timing = f"{same['timing'].iloc[0]}?"           # from Yahoo's timestamp; treated as unknown
        t_eff = timing if timing in ("bmo", "amc") else "unknown"
        summ = summarize_history(history_table(df, eh, int(c["runup_window_days"]), int(c["drift_window_days"]),
                                               int(c["history_quarters"])))
        raw = prov.estimates(t)
        rev = revision_state(raw, float(c["revisions_tolerance"]))
        rday = _reaction_day(row.date, t_eff)
        implied = prov.implied_move(t, rday)
        if implied:
            # Remove ordinary daily variance between now and expiry to isolate the earnings move.
            logret = np.log(df["close"]).diff().dropna().tail(int(c["options"]["baseline_vol_days"]))
            implied = {**implied, "daily_vol": float(logret.std()),
                       "sessions_to_expiry": sessions_between(price_as_of, implied["expiry"])}
            implied["event_move"] = event_implied_move(implied["move"], implied["daily_vol"],
                                                       implied["sessions_to_expiry"])
        ratio = implied_ratio(implied, summ.get("avg_abs_move", np.nan))
        exit_day = last_exit_day(row.date, t_eff)
        s_exit, s_react = sessions_between(price_as_of, exit_day), sessions_between(price_as_of, rday)
        score, parts = score_upcoming(summ, rev, ratio, c)
        neg = _negative_flags(t, prov, edgar, cfg, price_as_of)
        real_neg = [f for f in neg if "unavailable" not in f]
        score -= len(real_neg) * float(c["scoring"]["negative_catalyst_penalty"])
        play, why = choose_play(summ, rev, ratio, s_exit, c)

        gaps = []
        if implied is None:
            gaps.append("implied move unavailable")
        elif implied.get("event_move") is None:
            gaps.append("earnings-implied move not separable from normal volatility")
        elif implied.get("used_last") or (implied.get("spread_pct") or 0) > float(c["options"]["max_spread_pct"]):
            gaps.append("implied move unreliable (wide spread / stale last price)")
        if rev is None:
            gaps.append("estimate revisions unavailable")
        if t_eff == "unknown":
            gaps.append("report timing unknown (treated conservatively)")

        metrics = {"report_date": row.date.isoformat(), "timing": timing, "play_reason": "; ".join(why),
                   "quarters": summ.get("n", 0), "beat_rate": _r(summ.get("beat_rate")),
                   "avg_abs_move": _r(summ.get("avg_abs_move")), "straddle_move": _r(implied["move"]) if implied else None,
                   "implied_move": _r(implied.get("event_move")) if implied else None,
                   "options_expiry": implied["expiry"].isoformat() if implied else None,
                   "implied_ratio": _r(ratio, 2), "avg_runup": _r(summ.get("avg_runup")),
                   "runup_hit_rate": _r(summ.get("runup_hit_rate")), "avg_drift_after_beat": _r(summ.get("avg_drift_after_beat")),
                   "revisions": rev, **{f"pts_{k}": round(v, 1) for k, v in parts.items()}}
        base = {"ticker": t, "report_date": row.date, "timing": timing, "play": play, "score": round(score, 1),
                "why": "; ".join(why), "beat_rate": metrics["beat_rate"], "implied_ratio": metrics["implied_ratio"],
                "flags": "; ".join(neg + gaps)}
        if play == "avoid":
            rejects.append({"ticker": t, "list": "upcoming", "reason": "; ".join(why)})
            continue
        if play in ("post_drift", "runup_watch"):
            watch.append(base)
            continue
        close = float(df["close"].iloc[-1])
        atr = float(ind.atr(df, int(cfg.get("indicators.atr_period"))).iloc[-1])
        plan, err = plan_upcoming(play, close, atr, summ, implied, s_exit, s_react, c)
        if plan is None:
            rejects.append({"ticker": t, "list": "upcoming", "reason": err or "no plan"})
            continue
        if score < float(c["scoring"]["min_score"]):
            rejects.append({"ticker": t, "list": "upcoming", "reason": f"{play}: score {score:.1f} < {c['scoring']['min_score']}"})
            continue
        n = summ.get("n", 0)
        conf = (Confidence.high if n >= 10 and not gaps and not real_neg
                else Confidence.low if n < int(c["min_history_quarters"]) or len(gaps) >= 2 else Confidence.med)
        if play == "run_up":
            thesis = (f"Reports {row.date} ({timing}). Over the last {n} reports it rose into the print "
                      f"{summ['runup_hit_rate']:.0%} of the time, averaging {summ['avg_runup']:+.1%} over "
                      f"{c['runup_window_days']} sessions. Plan exits before the report, so no earnings gap risk.")
            hits = ["runup_pattern"] + (["revisions_up"] if rev == "up" else [])
            inval = f"Close below {plan.stop:.2f}, or still holding at the close on {exit_day} (exit before the report)."
        else:
            thesis = (f"Reports {row.date} ({timing}). Beat estimates {summ['beat_rate']:.0%} of the last {n} reports, "
                      f"estimates rising into the print, and options imply a {implied['event_move']:.1%} earnings move vs a "
                      f"{summ['avg_abs_move']:.1%} average actual move (cheap). Holding through carries gap risk.")
            hits = ["beat_history", "revisions_up", "implied_cheap"]
            inval = f"A miss or guide-down; any close below {plan.stop:.2f}."
        if (summ.get("avg_drift_after_beat") or 0) > 0:
            hits.append("positive_drift_history")
        signals.append(_signal(t, names.get(t, t), plan, score, conf, thesis, hits,
                               [KeyDate(date=row.date, kind="earnings", note=timing)], inval, neg + gaps,
                               _sources(t, price_as_of, date.today(), implied), metrics, price_as_of))
    return signals, watch, rejects


def drift_prefilter(df: pd.DataFrame, row, c: dict) -> str | None:
    """Reject from price data and the calendar's own surprise figure alone (no Yahoo calls).
    Unknown timing: the name survives if EITHER timing assumption could qualify."""
    d = c["drift"]
    if row.surprise_pct is not None and not pd.isna(row.surprise_pct) and row.surprise_pct < float(d["big_beat_pct"]):
        return f"surprise {row.surprise_pct:+.1f}% < {d['big_beat_pct']}% (calendar)"
    rvol = ind.relative_volume(df, 50)
    timings = [row.timing] if row.timing in ("bmo", "amc") else ["bmo", "amc"]
    reasons = []
    for tm in timings:
        ev = event_stats(df, row.date, tm, int(c["runup_window_days"]), int(c["drift_window_days"]), rvol)
        if ev is None:
            reasons.append("reaction day not in price data yet")
            continue
        fails = []
        if ev["gap"] < float(d["min_gap_pct"]):
            fails.append(f"gap {ev['gap']:+.1%}")
        if not (ev["rvol"] >= float(d["min_rvol"])):
            fails.append(f"volume {ev['rvol']:.1f}x")
        if ev["min_low_since"] <= ev["pre_close"]:
            fails.append("gap filled")
        if not fails:
            return None
        reasons.append(f"{tm}: " + ", ".join(fails))
    return "no qualifying reaction (" + "; ".join(reasons) + ")"


def analyze_drift(rows: pd.DataFrame, hist: dict, prov, edgar, cfg: Settings, price_as_of: date,
                  names: dict) -> tuple[list[Signal], list[dict]]:
    c = cfg.section("catalyst")
    signals, rejects = [], []
    lookback = int(c["lookback_trading_days"])
    for row in rows.itertuples():
        t = row.symbol
        df = hist.get(t)
        if df is None or len(df) < 60:
            rejects.append({"ticker": t, "list": "drift", "reason": "insufficient price history"})
            continue
        pre = drift_prefilter(df, row, c)
        if pre:
            rejects.append({"ticker": t, "list": "drift", "reason": pre})
            continue
        eh = prov.earnings_history(t)
        ev_row = eh[(eh["date"] == row.date) & eh["eps_reported"].notna()]
        surprise = float(ev_row["surprise_pct"].iloc[0]) if len(ev_row) else row.surprise_pct
        timing = ev_row["timing"].iloc[0] if len(ev_row) else (row.timing if row.timing != "unknown" else "amc")
        r = reaction_index(df.index, row.date, timing)
        if r is None:
            rejects.append({"ticker": t, "list": "drift", "reason": "reaction day not in price data yet"})
            continue
        ev = event_stats(df, row.date, timing, int(c["runup_window_days"]), int(c["drift_window_days"]),
                         ind.relative_volume(df, 50))
        if ev is None or ev["bars_since"] > lookback:
            rejects.append({"ticker": t, "list": "drift", "reason": "reported outside the lookback window"})
            continue
        raw = prov.estimates(t)
        guid = guidance_proxy(raw, float(c["drift"]["guidance_proxy_min_change"]))
        plan, fails, score, parts = evaluate_drift(ev, surprise, guid, c)
        if plan is None:
            rejects.append({"ticker": t, "list": "drift", "reason": "; ".join(fails)})
            continue
        neg = _negative_flags(t, prov, edgar, cfg, price_as_of)
        real_neg = [f for f in neg if "unavailable" not in f]
        score -= len(real_neg) * float(c["scoring"]["negative_catalyst_penalty"])
        if score < float(c["scoring"]["min_score"]):
            rejects.append({"ticker": t, "list": "drift", "reason": f"score {score:.1f} < {c['scoring']['min_score']}"})
            continue
        conf = Confidence.high if guid == "raised" and not real_neg else Confidence.low if guid is None else Confidence.med
        thesis = (f"Reported {row.date} ({timing}): EPS surprise {surprise:+.1f}%, gapped {ev['gap']:+.1%} on "
                  f"{ev['rvol']:.1f}x volume, and has held above the pre-report close {ev['pre_close']:.2f} for "
                  f"{ev['bars_since']} sessions. Forward estimates since the report: {guid or 'unknown'} "
                  f"(guidance proxy).")
        hits = ["big_beat", "gap_up", "heavy_volume", "gap_holding"] + (["guidance_raised_proxy"] if guid == "raised" else [])
        metrics = {"report_date": row.date.isoformat(), "timing": timing, "surprise_pct": _r(surprise, 2),
                   "gap": _r(ev["gap"]), "reaction_rvol": _r(ev["rvol"], 2), "pre_close": _r(ev["pre_close"], 2),
                   "sessions_since_reaction": int(ev["bars_since"]), "guidance_proxy": guid,
                   **{f"pts_{k}": round(v, 1) for k, v in parts.items()}}
        inval = f"Close back below the reaction-day low {plan.stop:.2f} (gap failing) or into the gap at {ev['pre_close']:.2f}."
        signals.append(_signal(t, names.get(t, t), plan, score, conf, thesis, hits,
                               [KeyDate(date=row.date, kind="earnings", note="reported")], inval, neg,
                               _sources(t, price_as_of, date.today(), None), metrics, price_as_of))
    return signals, rejects


def run(cfg: Settings | None = None, source: str | None = None, limit: int | None = None,
        price_provider=None, calendar=None, use_edgar: bool | None = None) -> dict:
    from core.providers.earnings_calendar import EarningsCalendarProvider
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    c = cfg.section("catalyst")
    prov = price_provider or YFinanceProvider(cfg)
    cal = calendar or EarningsCalendarProvider(cfg)
    uni = build_universe(cfg, source=source, limit=limit, price_provider=prov)
    passed = uni.passed.set_index("symbol")
    price_as_of = max(df.index[-1] for t, df in uni.histories.items() if t in passed.index).date()

    edgar = None
    if use_edgar is not False and cfg.env("providers.edgar.user_agent_env"):
        from core.providers.edgar import EdgarProvider
        edgar = EdgarProvider(cfg)

    def pick(df: pd.DataFrame) -> pd.DataFrame:
        df = df[df["symbol"].isin(passed.index)].copy()
        df["adv"] = passed.loc[df["symbol"], "avg_dollar_volume"].to_numpy()
        return df.sort_values("adv", ascending=False).drop_duplicates("symbol").head(int(c["max_candidates"]))

    upcoming = pick(cal.range(price_as_of + timedelta(days=1), price_as_of + timedelta(days=int(c["lookahead_days"]))))
    lb = int(c["lookback_trading_days"])
    recent = pick(cal.range((pd.Timestamp(price_as_of) - pd.offsets.BDay(lb + 1)).date(), price_as_of))
    log.info("%d upcoming and %d recent reporters in the liquid universe", len(upcoming), len(recent))

    tickers = sorted(set(upcoming["symbol"]) | set(recent["symbol"]))
    hist = prov.history(tickers, c["history_period"]) if tickers else {}
    names = passed["name"].to_dict()

    up_sigs, watch, rej_up = analyze_upcoming(upcoming, hist, prov, edgar, cfg, price_as_of, names)
    dr_sigs, rej_dr = analyze_drift(recent, hist, prov, edgar, cfg, price_as_of, names)
    signals = sorted(up_sigs + dr_sigs, key=lambda s: s.score, reverse=True)[: int(c["output_top_n"])]
    watch = sorted(watch, key=lambda w: w["score"], reverse=True)
    rejects = rej_up + rej_dr

    out = cfg.path_for("paths.reports_dir")
    stem = f"catalyst-{date.today().isoformat()}"
    jp, mp = save_signals(signals, out, stem)
    pd.DataFrame(watch).to_csv(out / f"{stem}-watch.csv", index=False)
    pd.DataFrame(rejects, columns=["ticker", "list", "reason"]).to_csv(out / f"{stem}-rejected.csv", index=False)
    _write_header(mp, signals, watch, rejects, upcoming, recent, price_as_of, cal, edgar, cfg)
    return {"signals": signals, "watch": watch, "rejects": rejects, "price_as_of": price_as_of,
            "paths": {"json": jp, "md": mp}, "calendar_errors": cal.errors, "edgar": edgar is not None}


def _write_header(md_path, signals, watch, rejects, upcoming, recent, price_as_of, cal, edgar, cfg) -> None:
    body = md_path.read_text(encoding="utf-8")
    c = cfg.section("catalyst")
    L = [f"# Catalyst plays — {date.today().isoformat()}", "",
         f"_Research only, not investment advice. Prices as of close {price_as_of}. "
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC._", "",
         f"- Upcoming reporters analyzed (next {c['lookahead_days']} days, most liquid {c['max_candidates']}): {len(upcoming)}",
         f"- Recent reporters checked for drift (last {c['lookback_trading_days']} sessions): {len(recent)}",
         f"- Actionable plays: **{len(signals)}**; watchlist: {len(watch)}; rejected: {len(rejects)}",
         "- Offering filings check: " + ("on (SEC EDGAR)" if edgar else "**off** (set SEC_USER_AGENT to enable)")]
    if cal.errors:
        L.append(f"- Calendar fetch errors: {', '.join(cal.errors[:5])}")
    L += ["", "## Actionable plays", "", body]
    if watch:
        L += ["## Watchlist: wait for the report (post-earnings drift is the default play)", "",
              "Trade only if it beats, gaps up on volume and holds the gap; the drift scan will pick it up.", "",
              "| Ticker | Reports | Timing | Play | Score | Beat rate | Implied/actual | Why | Flags |", "|---|---|---|---|---|---|---|---|---|"]
        for w in watch:
            br = f"{w['beat_rate']:.0%}" if w["beat_rate"] is not None else "—"
            ir = f"{w['implied_ratio']:.2f}" if w["implied_ratio"] is not None else "—"
            L.append(f"| {w['ticker']} | {w['report_date']} | {w['timing']} | {w['play']} | {w['score']:.0f} | {br} | {ir} | {w['why']} | {w['flags'] or '—'} |")
        L.append("")
    if rejects:
        L += ["## Rejected", "", "| Ticker | List | Reason |", "|---|---|---|"]
        L += [f"| {r['ticker']} | {r['list']} | {r['reason']} |" for r in rejects]
        L.append("")
    L += ["## Not covered yet", ""] + [f"- {x}" for x in NOT_COVERED]
    md_path.write_text("\n".join(L) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Catalyst plays (research only).")
    ap.add_argument("--source", choices=["list", "nasdaqtrader"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    res = run(source=args.source, limit=args.limit)
    print(f"\n{len(res['signals'])} actionable plays (prices as of {res['price_as_of']}) -> {res['paths']['md']}")
    for s in res["signals"]:
        m = s.metrics
        print(f"  {s.ticker:<6} {s.score:5.1f} {s.confidence.value:<4} {m['play']:<17} report {m['report_date']} "
              f"entry {s.entry_zone.low:.2f}-{s.entry_zone.high:.2f} stop {s.stop:.2f} target {s.targets[0]:.2f} "
              f"R:R {s.reward_risk:.1f}")
    print(f"Watchlist: {len(res['watch'])}  Rejected: {len(res['rejects'])}  "
          f"EDGAR: {'on' if res['edgar'] else 'off'}  Calendar errors: {len(res['calendar_errors'])}")


if __name__ == "__main__":
    main()
