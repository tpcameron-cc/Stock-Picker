"""run-all-scans: regime check -> four scanners in parallel -> merge by ticker -> size -> journal -> daily report.

    uv run python -m scanners.run_all                    # full run
    uv run python -m scanners.run_all --quick            # cached data, first N listed stocks, top 5
    uv run python -m scanners.run_all --only breakout-scanner --only catalyst-plays
    uv run python -m scanners.run_all --skip-run         # merge today's existing scanner outputs only

Scanners run as separate processes (the parallel "subagents" of this pipeline): one failing doesn't stop the others,
and only outputs written during this run are used. Writes reports/YYYY-MM-DD.md and .csv. Research only; no orders.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from core import journal
from core.config import PROJECT_ROOT, Settings, settings as default_settings
from core.providers.base import STALE_OK_ENV
from core.regime import Regime, current_regime
from core.risk import SizingDecision, size_signals
from core.signals import Signal, load_signals
from scanners.catalyst.plays import sessions_between

log = logging.getLogger(__name__)

SKILLS = {  # skill -> (module, report prefix, accepts --limit)
    "political-edge": ("scanners.political.scan", "political", False),
    "breakout-scanner": ("scanners.breakout.scan", "breakout", True),
    "momentum-hype": ("scanners.hype.scan", "hype", False),
    "catalyst-plays": ("scanners.catalyst.scan", "catalyst", True),
}
TECHNICAL = {"breakout-scanner", "momentum-hype"}    # skills that don't look at the earnings calendar themselves


@dataclass
class Idea:
    ticker: str
    primary: Signal
    contributors: list[Signal]
    score: float
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rejected: str | None = None
    decision: SizingDecision | None = None

    @property
    def skills(self) -> list[str]:
        return [s.skill for s in self.contributors]


# ---------------------------------------------------------------------------
# Running the scanners
# ---------------------------------------------------------------------------

def launch(skills: list[str], quick: bool, cfg: Settings, log_dir: Path) -> dict[str, dict]:
    o = cfg.section("orchestrator")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if quick:
        env[STALE_OK_ENV] = "1"
    procs = {}
    log_dir.mkdir(parents=True, exist_ok=True)
    for sk in skills:
        module, _, takes_limit = SKILLS[sk]
        args = [sys.executable, "-m", module]
        if quick and takes_limit:
            args += ["--limit", str(int(o["quick_limit"]))]
        lp = log_dir / f"{sk}.log"
        fh = open(lp, "w", encoding="utf-8")
        procs[sk] = (subprocess.Popen(args, cwd=PROJECT_ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT), fh, lp,
                     _time.monotonic())
        log.info("started %s", sk)
    out = {}
    deadline = _time.monotonic() + 60 * float(o["timeout_minutes"])
    for sk, (p, fh, lp, t0) in procs.items():
        try:
            rc = p.wait(timeout=max(1.0, deadline - _time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            rc = "timeout"
        fh.close()
        out[sk] = {"returncode": rc, "log": lp, "seconds": round(_time.monotonic() - t0)}
        log.info("%s finished: %s in %ss", sk, rc, out[sk]["seconds"])
    return out


def load_outputs(skills: list[str], cfg: Settings, day: date, since: float | None) -> tuple[dict, dict]:
    """Signals per skill from today's reports. With `since`, files older than the run start are ignored."""
    rd = cfg.path_for("paths.reports_dir")
    sigs, status = {}, {}
    for sk in skills:
        p = rd / f"{SKILLS[sk][1]}-{day.isoformat()}.json"
        if not p.exists():
            sigs[sk], status[sk] = [], "no output file"
            continue
        if since is not None and p.stat().st_mtime < since:
            sigs[sk], status[sk] = [], "no fresh output (file predates this run)"
            continue
        sigs[sk] = load_signals(p)
        status[sk] = "ok"
    return sigs, status


def _read_csv(p: Path) -> pd.DataFrame | None:
    """None if missing or empty (a scanner with nothing to list writes an empty file)."""
    try:
        return pd.read_csv(p) if p.exists() else None
    except pd.errors.EmptyDataError:
        return None


def conflict_context(cfg: Settings, day: date, tickers: set[str], edgar) -> dict:
    """Earnings dates (catalyst outputs + key dates), 'estimates falling' names, insider sells."""
    rd = cfg.path_for("paths.reports_dir")
    earnings: dict[str, date] = {}
    falling: set[str] = set()
    watch = rd / f"catalyst-{day.isoformat()}-watch.csv"
    w = _read_csv(watch)
    if w is not None and "report_date" in w:
        for r in w.itertuples():
            earnings[r.ticker] = date.fromisoformat(str(r.report_date)[:10])
    rej = rd / f"catalyst-{day.isoformat()}-rejected.csv"
    r = _read_csv(rej)
    if r is not None and "reason" in r:
        falling = set(r.loc[r["reason"].fillna("").str.contains("estimates falling", case=False), "ticker"])
    sells: dict[str, int] | None = None
    if edgar is not None:
        sells = {}
        for t in tickers:
            try:
                sells[t] = len(edgar.insider_transactions(t, "S", 30, day))
            except Exception:
                pass
    return {"earnings": earnings, "estimates_falling": falling, "insider_sells": sells}


# ---------------------------------------------------------------------------
# Merge (pure)
# ---------------------------------------------------------------------------

def merge_ideas(sigs: dict[str, list[Signal]], ctx: dict, regime: Regime, cfg: Settings, asof: date) -> list[Idea]:
    o = cfg.section("orchestrator")
    by_ticker: dict[str, list[Signal]] = {}
    for sk, lst in sigs.items():
        for s in lst:
            by_ticker.setdefault(s.ticker, []).append(s)
    for s in [x for lst in sigs.values() for x in lst]:          # earnings dates carried on any signal
        for k in s.key_dates:
            if k.kind == "earnings" and k.date >= asof:
                ctx["earnings"].setdefault(s.ticker, k.date)

    threshold = float(o["min_score"]) + regime.min_score_add
    ideas = []
    for t, group in by_ticker.items():
        group = sorted(group, key=lambda s: s.score, reverse=True)
        primary = group[0]
        skills = list(dict.fromkeys(s.skill for s in group))
        idea = Idea(t, primary, group, primary.score + float(o["confluence_bonus"]) * (len(skills) - 1))
        if len(skills) > 1:
            idea.notes.append(f"confluence: flagged by {', '.join(skills)} (+{float(o['confluence_bonus']) * (len(skills) - 1):g})")
        ed = ctx["earnings"].get(t)
        if ed is not None and skills and set(skills) & TECHNICAL:
            n = sessions_between(asof, ed)
            if 0 <= n <= int(o["earnings_conflict_sessions"]):
                idea.conflicts.append(f"earnings {ed} in {n} sessions vs a technical "
                                      f"{'/'.join(sorted(set(skills) & TECHNICAL))} idea")
        if t in ctx["estimates_falling"] and "catalyst-plays" not in skills:
            idea.conflicts.append("EPS estimates falling into the next report (catalyst-plays)")
        if "momentum-hype" in skills:
            if ctx["insider_sells"] is None:
                idea.notes.append("insider selling not checked (set SEC_USER_AGENT)")
            elif ctx["insider_sells"].get(t, 0) > 0:
                idea.conflicts.append(f"hype with insider selling: {ctx['insider_sells'][t]} Form 4 sales in 30 days")
        idea.score = min(100.0, idea.score - float(o["conflict_penalty"]) * len(idea.conflicts))
        if idea.score < threshold:
            idea.rejected = f"score {idea.score:.1f} < {threshold:g} (min {o['min_score']} + regime {regime.min_score_add:g})"
        ideas.append(idea)
    return sorted(ideas, key=lambda i: i.score, reverse=True)


def merged_signal(idea: Idea) -> Signal:
    """The primary signal with the merged score, union of signals and the extra context in its flags."""
    p = idea.primary
    others = [s for s in idea.contributors[1:] if s.skill != p.skill]
    thesis = p.thesis + "".join(f" Also {s.skill}: {s.thesis.split('. ')[0].rstrip('.')}." for s in others)
    return p.model_copy(update={
        "score": max(0.0, min(100.0, idea.score)), "thesis": thesis,
        "signals_hit": list(dict.fromkeys(h for s in idea.contributors for h in s.signals_hit)),
        "risk_flags": [f"CONFLICT: {c}" for c in idea.conflicts] + idea.notes + p.risk_flags,
        "metrics": {**p.metrics, "skills": ", ".join(idea.skills)}})


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(cfg: Settings | None = None, skills: list[str] | None = None, quick: bool = False, skip_run: bool = False,
        do_log: bool = True, today: date | None = None) -> dict:
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    o = cfg.section("orchestrator")
    skills = skills or list(o["skills"])
    today = today or date.today()
    if quick:
        os.environ[STALE_OK_ENV] = "1"
    prov = YFinanceProvider(cfg)

    regime = current_regime(cfg, prov)
    log.info(regime.headline())

    started = _time.time()
    runs = {} if skip_run else launch(skills, quick, cfg, cfg.path_for("paths.cache_dir").parent / "logs")
    sigs, status = load_outputs(skills, cfg, today, None if skip_run else started - 1)
    for sk, r in runs.items():
        if r["returncode"] != 0:
            status[sk] = f"FAILED ({r['returncode']}); see {r['log']}"

    edgar = None
    if cfg.env("providers.edgar.user_agent_env"):
        from core.providers.edgar import EdgarProvider
        edgar = EdgarProvider(cfg)
    hype_tickers = {s.ticker for s in sigs.get("momentum-hype", [])}
    ctx = conflict_context(cfg, today, hype_tickers, edgar)
    asof = max((date.fromisoformat(s.metrics["price_as_of"]) for lst in sigs.values() for s in lst
                if s.metrics.get("price_as_of")), default=today)
    ideas = merge_ideas(sigs, ctx, regime, cfg, asof)

    # Size the surviving ideas against the journal's book, with the regime multiplier.
    jpath = journal.db_path(cfg)
    journal.update_outcomes(jpath, prov, cfg)
    book = journal.open_positions(jpath)
    # An idea identical to one already TAKEN in the journal (same price date, ticker, skill) is the same paper trade
    # re-scanned: keep its original size, don't re-size it; it stays in the book and counts against the limits.
    held = {(r.signal_date, r.ticker, r.skill): r for r in book.itertuples()} if len(book) else {}
    survivors = []
    for i in ideas:
        if i.rejected:
            continue
        key = (journal.signal_date_of(i.primary).isoformat(), i.ticker, i.primary.skill)
        if key in held:
            h = held[key]
            i.decision = SizingDecision(i.ticker, i.primary.skill, i.score, True, round(i.primary.reward_risk, 2),
                                        shares=None if pd.isna(h.shares) else int(h.shares),
                                        risk_usd=None if pd.isna(h.risk_usd) else float(h.risk_usd),
                                        flags=[f"already in your paper book (logged {str(h.logged_at)[:10]}, "
                                               f"{h.status}); original size kept, not re-sized"])
            i.notes.append("already held")
            continue
        survivors.append(i)
    merged = [merged_signal(i) for i in survivors]
    tickers = sorted({s.ticker for s in merged} | set(book["ticker"]))
    hist = prov.history(tickers, "1y") if tickers else {}
    rets = {t: df["close"].pct_change().dropna() for t, df in hist.items()}
    existing = [{"ticker": t, "sector": prov.sector(t)} for t in book["ticker"]]
    decisions = size_signals(merged, cfg, existing, prov.sector, rets, multiplier=regime.size_multiplier)
    dmap = {d.ticker: d for d in decisions}
    for i in survivors:
        i.decision = dmap.get(i.ticker)

    logged = {"inserted": 0, "skipped_duplicates": 0}
    if do_log:
        per_signal: dict = {}
        for i in ideas:
            for s in i.contributors:
                if i.decision is not None and s is i.primary:
                    per_signal[(s.ticker, s.skill)] = i.decision
                else:
                    why = i.rejected or (f"confluence: sized via {i.primary.skill}" if s is not i.primary else "not sized")
                    per_signal[(s.ticker, s.skill)] = SizingDecision(s.ticker, s.skill, s.score, False,
                                                                     round(s.reward_risk, 2), reasons=[why])
        all_sigs = [s for lst in sigs.values() for s in lst]
        logged = journal.log_signals(jpath, all_sigs, per_signal,
                                     log_rejected=bool(cfg.get("journal.log_rejected", True)))

    paths = write_report(cfg, today, regime, ideas, sigs, status, runs, quick, logged, asof)
    return {"regime": regime, "ideas": ideas, "status": status, "runs": runs, "logged": logged, "paths": paths}


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def write_report(cfg, today, regime: Regime, ideas: list[Idea], sigs, status, runs, quick, logged, asof) -> dict:
    o = cfg.section("orchestrator")
    top_n = int(o["quick_top_n"] if quick else o["top_n"])
    out = cfg.path_for("paths.reports_dir")
    approved = [i for i in ideas if i.decision is not None and i.decision.approved]
    L = [f"# Daily scan — {today.isoformat()}", "",
         f"_Research only, not investment advice. No orders are placed. Prices as of close {asof}. "
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC."
         + (" **Quick mode: cached data (possibly stale), partial universe, top 5.**" if quick else "") + "_", "",
         f"> **{regime.headline()}**", ""]
    L += [f"- {k}: {v if v is None else round(v, 3)}" for k, v in regime.detail.items()]
    L += [f"- note: {n}" for n in regime.notes]
    L += ["", "## Scanners", "", "| Skill | Status | Ideas | Runtime | Report |", "|---|---|---|---|---|"]
    for sk in sigs:
        rt = f"{runs[sk]['seconds']}s" if sk in runs else "—"
        L.append(f"| {sk} | {status.get(sk)} | {len(sigs[sk])} | {rt} | reports/{SKILLS[sk][1]}-{today.isoformat()}.md |")
    L += ["", f"Merged into {len(ideas)} tickers; {sum(1 for i in ideas if not i.rejected)} passed the score threshold; "
          f"**{len(approved)} approved by risk-and-sizing**. Journal: {logged['inserted']} logged, "
          f"{logged['skipped_duplicates']} already present.", ""]

    L += [f"## Top {top_n} ideas", ""]
    ranked = [i for i in ideas if not i.rejected][:top_n]
    if not ranked:
        L.append("_No idea cleared the threshold today._")
    for n, i in enumerate(ranked, 1):
        s, d = merged_signal(i), i.decision
        size = (f"already held: {d.shares} shares, risk ${d.risk_usd:,.0f} (original size)" if "already held" in i.notes and d
                else f"**{d.shares} shares** (${d.notional:,.0f}, risk ${d.risk_usd:,.0f} = {d.risk_pct_of_account:.2%} of account)"
                if d and d.approved and d.shares is not None else
                ("approved (account size not set)" if d and d.approved else f"not taken: {'; '.join(d.reasons) if d else 'not sized'}"))
        L += [f"### {n}. {i.ticker} — {s.company}  (score {s.score:.0f}, {s.confidence.value}; {', '.join(i.skills)})", "",
              s.thesis, "",
              f"- **Plan:** entry {s.entry_zone.low:.2f}–{s.entry_zone.high:.2f}, stop {s.stop:.2f}, target(s) "
              f"{', '.join(f'{x:.2f}' for x in s.targets)}, horizon {s.horizon_days}d, R:R {s.reward_risk:.2f}",
              f"- **Size:** {size}"]
        if s.key_dates:
            L.append("- **Key dates:** " + "; ".join(f"{k.date} {k.kind}" for k in s.key_dates))
        if i.conflicts:
            L.append("- **Conflicts:** " + "; ".join(i.conflicts))
        flags = [f for f in s.risk_flags if not f.startswith("CONFLICT")]
        if d and d.flags:
            flags += d.flags
        if flags:
            L.append("- **Flags:** " + "; ".join(dict.fromkeys(flags)))
        L.append("- **Sources:** " + "; ".join(f"[{x.url.host}]({x.url}) as of {x.as_of}" for x in s.sources))
        L.append("")

    L += ["## By skill", ""]
    for sk, lst in sigs.items():
        L += [f"### {sk} ({len(lst)})", ""]
        if not lst:
            L += [f"_{status.get(sk)}_" if status.get(sk) != "ok" else "_No ideas._", ""]
            continue
        L += ["| Ticker | Score | Setup | Merged score | Outcome |", "|---|---|---|---|---|"]
        imap = {i.ticker: i for i in ideas}
        for s in sorted(lst, key=lambda x: x.score, reverse=True):
            i = imap[s.ticker]
            outcome = i.rejected or ("approved" if i.decision and i.decision.approved and s is i.primary
                                     else f"via {i.primary.skill}" if s is not i.primary
                                     else "; ".join(i.decision.reasons) if i.decision else "—")
            L.append(f"| {s.ticker} | {s.score:.0f} | {s.metrics.get('setup', '')} | {i.score:.0f} | {outcome} |")
        L.append("")

    rej = [i for i in ideas if i.rejected or (i.decision and not i.decision.approved)]
    if rej:
        L += ["## Rejected", "", "| Ticker | Skills | Score | Reason |", "|---|---|---|---|"]
        for i in rej:
            why = i.rejected or "; ".join(i.decision.reasons)
            L.append(f"| {i.ticker} | {', '.join(i.skills)} | {i.score:.0f} | {why} |")
    md = out / f"{today.isoformat()}.md"
    md.write_text("\n".join(L) + "\n", encoding="utf-8")

    rows = []
    for i in ideas:
        s, d = merged_signal(i), i.decision
        rows.append({"ticker": i.ticker, "company": s.company, "skills": ", ".join(i.skills), "score": round(i.score, 1),
                     "confidence": s.confidence.value, "rejected": i.rejected, "approved": bool(d and d.approved),
                     "shares": d.shares if d else None, "notional": d.notional if d else None,
                     "risk_usd": d.risk_usd if d else None, "entry_low": s.entry_zone.low, "entry_high": s.entry_zone.high,
                     "stop": s.stop, "targets": " / ".join(f"{x:.2f}" for x in s.targets), "horizon_days": s.horizon_days,
                     "reward_risk": round(s.reward_risk, 2), "conflicts": "; ".join(i.conflicts),
                     "sizing_reasons": "; ".join(d.reasons) if d else "", "regime": regime.label})
    csv = out / f"{today.isoformat()}.csv"
    pd.DataFrame(rows).to_csv(csv, index=False)
    return {"md": md, "csv": csv}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Run all scanners, merge, size and journal (research only).")
    ap.add_argument("--only", action="append", choices=list(SKILLS), help="run only this skill (repeatable)")
    ap.add_argument("--quick", action="store_true", help="cached data, partial universe, top 5")
    ap.add_argument("--skip-run", action="store_true", help="don't run scanners; merge today's existing outputs")
    ap.add_argument("--no-log", action="store_true", help="don't write to the paper journal")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    res = run(skills=args.only, quick=args.quick, skip_run=args.skip_run, do_log=not args.no_log)
    print("\n" + res["regime"].headline())
    for sk, st in res["status"].items():
        print(f"  {sk}: {st}")
    ok = [i for i in res["ideas"] if not i.rejected]
    print(f"\n{len(res['ideas'])} tickers, {len(ok)} above threshold, "
          f"{sum(1 for i in ok if i.decision and i.decision.approved)} approved -> {res['paths']['md']}")
    for i in ok[:10]:
        d = i.decision
        print(f"  {i.ticker:<6} {i.score:5.1f} {', '.join(i.skills):<35} "
              + (f"{d.shares} sh" if d and d.approved and d.shares is not None else ("approved" if d and d.approved else
                 f"no: {'; '.join(d.reasons)[:60]}" if d else "")))
    print(f"Journal: {res['logged']}")


if __name__ == "__main__":
    main()
