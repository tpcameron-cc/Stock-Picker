"""review-performance: weekly read of the paper journal. Marks outcomes, measures each skill and signal type,
says plainly which skills add value, and SUGGESTS threshold changes (never applies them).

    uv run python -m core.review              # update outcomes from prices, then review
    uv run python -m core.review --no-update  # review the journal as it is

Writes reports/review-YYYY-MM-DD.md.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from core import journal
from core.config import Settings, settings as default_settings

# Where each skill's score threshold lives (for suggestions only; nothing here edits settings).
SKILL_MIN_SCORE_KEY = {"breakout-scanner": "breakout.scoring.min_score", "catalyst-plays": "catalyst.scoring.min_score",
                       "political-edge": "political.scoring.min_score", "momentum-hype": "hype.min_score"}


def bootstrap_ci(r: pd.Series, samples: int, seed: int = 0, level: float = 0.90) -> tuple[float, float]:
    """Percentile bootstrap CI of the mean R."""
    x = r.dropna().to_numpy(float)
    if len(x) == 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(samples, len(x)), replace=True).mean(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def verdict(n: int, ci: tuple[float, float], rv: dict) -> str:
    if n < int(rv["min_closed_trades"]):
        return f"too early ({n} closed; need {rv['min_closed_trades']})"
    band = float(rv["noise_band_r"])
    lo, hi = ci
    if lo > band:
        return "adding value"
    if hi < band:
        return "negative: losing money"
    return "inconclusive: indistinguishable from noise"


def skill_table(closed: pd.DataFrame, rv: dict) -> pd.DataFrame:
    rows = {}
    for sk, g in closed.groupby("skill"):
        st = journal.trade_stats(g)
        ci = bootstrap_ci(g["r_multiple"], int(rv["bootstrap_samples"]))
        rows[sk] = {**st, "exp_r_ci_low": ci[0], "exp_r_ci_high": ci[1], "verdict": verdict(len(g), ci, rv)}
    return pd.DataFrame(rows).T


def suggestions(closed_all: pd.DataFrame, skills: pd.DataFrame, by_signal: pd.DataFrame, rv: dict) -> list[str]:
    """Rule-based, data-backed suggestions. Returned as text for the user to approve; never applied."""
    out: list[str] = []
    need = int(rv["min_closed_trades"])
    for sk, r in skills.iterrows():
        if r["trades"] >= need and r["exp_r_ci_high"] < float(rv["noise_band_r"]):
            out.append(f"{sk}: expectancy {r['expectancy_r']:+.2f}R over {int(r['trades'])} trades (90% CI "
                       f"{r['exp_r_ci_low']:+.2f} to {r['exp_r_ci_high']:+.2f}). Raise `{SKILL_MIN_SCORE_KEY.get(sk, '?')}` "
                       f"by 10, or pause the skill.")
    if len(by_signal):
        for sig, r in by_signal.iterrows():
            if r["trades"] >= max(need // 2, 1) and r["expectancy_r"] < float(rv["bad_signal_r"]):
                out.append(f"signal '{sig}': {r['expectancy_r']:+.2f}R over {int(r['trades'])} trades. Lower its scoring "
                           f"weight in settings or require it to appear with another signal.")
    taken, rej = closed_all[closed_all["taken"] == 1], closed_all[closed_all["taken"] == 0]
    if len(rej) >= max(need // 2, 1) and len(taken) >= max(need // 2, 1):
        dt, dr = taken["r_multiple"].mean(), rej["r_multiple"].mean()
        if dr > dt + 0.2:
            out.append(f"ideas risk-and-sizing REJECTED ({dr:+.2f}R, {len(rej)}) beat the ones it took ({dt:+.2f}R, "
                       f"{len(taken)}): the filters may be too strict. Review `risk.min_reward_risk` and the sector cap.")
        elif dt > dr + 0.2:
            out.append(f"risk filters are helping: taken ideas {dt:+.2f}R vs rejected {dr:+.2f}R. Keep them.")
    if len(closed_all) >= need:
        ts = closed_all[closed_all["exit_reason"] == "time"]
        if len(ts) / len(closed_all) > 0.5 and ts["r_multiple"].mean() > 0:
            out.append(f"{len(ts) / len(closed_all):.0%} of trades end on the time stop with positive R "
                       f"({ts['r_multiple'].mean():+.2f}): horizons may be too short.")
    return out


def run(cfg: Settings | None = None, update: bool = True, today: date | None = None) -> dict:
    cfg = cfg or default_settings()
    rv = cfg.section("review")
    today = today or date.today()
    jpath = journal.db_path(cfg)
    updated = journal.update_outcomes(jpath, cfg=cfg) if update else pd.DataFrame()
    t = journal.read(jpath)
    closed_all = t[t["status"] == "closed"]
    closed_taken = closed_all[closed_all["taken"] == 1]
    perf = journal.performance(t, taken_only=False)          # idea quality: every logged idea, taken or not
    skills = skill_table(closed_all, rv) if len(closed_all) else pd.DataFrame()
    sugg = suggestions(closed_all, skills, perf.get("by_signal", pd.DataFrame()), rv) if len(closed_all) else []
    path = write_report(cfg, today, t, closed_all, closed_taken, perf, skills, sugg, updated)
    return {"journal": t, "skills": skills, "suggestions": sugg, "path": path, "updated": len(updated)}


def _pct(x):
    return "—" if x is None or pd.isna(x) else f"{x:+.1%}"


def _r(x):
    return "—" if x is None or pd.isna(x) else f"{x:+.2f}R"


def write_report(cfg, today, t, closed_all, closed_taken, perf, skills, sugg, updated) -> object:
    rv = cfg.section("review")
    out = cfg.path_for("paths.reports_dir") / f"review-{today.isoformat()}.md"
    L = [f"# Performance review — {today.isoformat()}", "",
         f"_Paper trades only. Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Outcomes use the same fill rules "
         f"as the backtest (core/simulate.py). Nothing in settings has been changed._", "",
         f"- Journal: {len(t)} ideas logged; outcomes refreshed for {len(updated)} active ideas",
         f"- Status: " + (", ".join(f"{k} {v}" for k, v in t["status"].value_counts().items()) if len(t) else "empty"),
         f"- Closed: {len(closed_all)} ({len(closed_taken)} taken, {len(closed_all) - len(closed_taken)} rejected-but-tracked)",
         ""]
    L += ["## Verdict by skill", ""]
    if len(skills):
        L += ["| Skill | Closed | Win rate | Avg win | Avg loss | Expectancy | 90% CI | vs SPY | Verdict |", "|---|---|---|---|---|---|---|---|---|"]
        for sk, r in skills.iterrows():
            L.append(f"| {sk} | {int(r['trades'])} | {r['win_rate']:.0%} | {_pct(r['avg_win'])} | {_pct(r['avg_loss'])} | "
                     f"{_r(r['expectancy_r'])} ({_pct(r['expectancy_pct'])}) | {_r(r['exp_r_ci_low'])} to {_r(r['exp_r_ci_high'])} | "
                     f"{_pct(r['excess_vs_spy'])} | **{r['verdict']}** |")
    else:
        L.append(f"_No closed paper trades yet. Every skill: too early to judge (need {rv['min_closed_trades']} closed trades each)._")
    pending = sorted(set(t["skill"]) - set(skills.index)) if len(t) else []
    if pending:
        L.append(f"\nSkills with ideas but no closed trades yet: {', '.join(pending)}.")
    for name, title in [("by_signal", "By signal type"), ("by_setup", "By setup")]:
        df = perf.get(name)
        if df is not None and len(df):
            L += ["", f"## {title} (all closed ideas)", "", "| | Trades | Win rate | Expectancy | vs SPY |", "|---|---|---|---|---|"]
            for k, r in df.sort_values("trades", ascending=False).iterrows():
                L.append(f"| {k} | {int(r['trades'])} | {r['win_rate']:.0%} | {_r(r['expectancy_r'])} | {_pct(r['excess_vs_spy'])} |")
    if len(closed_all):
        L += ["", "## Did risk-and-sizing help?", ""]
        for lab, g in [("taken", closed_all[closed_all["taken"] == 1]), ("rejected (tracked)", closed_all[closed_all["taken"] == 0])]:
            L.append(f"- {lab}: {len(g)} closed, expectancy {_r(g['r_multiple'].mean() if len(g) else np.nan)}")
    live = t[t["status"].isin(["open", "pending"])] if len(t) else t
    if len(live):
        L += ["", "## Open and pending paper trades", "", "| Ticker | Skill | Taken | Status | Filled | Unrealized |", "|---|---|---|---|---|---|"]
        for r in live.itertuples():
            L.append(f"| {r.ticker} | {r.skill} | {'yes' if r.taken else 'no'} | {r.status} | {r.fill_date or '—'} | "
                     f"{_r(r.r_multiple)} |")
    L += ["", "## Suggestions (not applied — say which to apply)", ""]
    L += [f"- {s}" for s in sugg] or ["- None yet: not enough closed trades to justify any change."]
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Weekly paper-trading review (suggestions only).")
    ap.add_argument("--no-update", action="store_true", help="don't refresh outcomes from prices")
    args = ap.parse_args(argv)
    res = run(update=not args.no_update)
    t = res["journal"]
    print(f"Journal: {len(t)} ideas; refreshed {res['updated']}. Report: {res['path']}")
    if len(res["skills"]):
        print(res["skills"][["trades", "win_rate", "expectancy_r", "exp_r_ci_low", "exp_r_ci_high", "verdict"]].to_string())
    else:
        print("No closed trades yet: too early to judge any skill.")
    for s in res["suggestions"]:
        print(" -", s)


if __name__ == "__main__":
    main()
