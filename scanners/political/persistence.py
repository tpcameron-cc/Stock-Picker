"""Does a member's past copy-trade performance predict their NEXT purchases? Walk-forward test. Read-only.

    uv run python -m scanners.political.persistence [--years 4]

Every `step_days` from (first data + the screen window) onward is a formation date F. At F each member is screened
as the live scan would have screened them then: the same copy-trade stats (screen.member_stats) on trades FILED by F
(a trade made before F but disclosed after it wasn't known at F) and the same criteria. Then every stock purchase
the member FILES in (F, F + step] is followed: entry at the close of the first session AFTER the filing date, held
`horizon_days` sessions, excess = stock return - SPY return over the same dates. Purchases without a full horizon of
prices yet are counted but left out.

Comparisons (buy-weighted mean forward excess; cluster bootstrap that resamples MEMBERS, so one prolific trader
can't manufacture significance):
  A. active members (>= min_trades at F): beat SPY at F vs trailed (trade-date prices)  -- the gate's raw signal
  B. same, on the delay-adjusted excess (filing-date prices)                         -- the score-mode signal
  C. members passing every non-performance criterion: kept vs dropped by the beat-SPY gate -- the live gate exactly
plus a rank correlation between formation excess (delay-adjusted) and each member-quarter's mean forward excess.

Nothing here changes settings; the report says what the evidence supports and the user decides.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.config import Settings, settings as default_settings
from scanners.political.screen import amount_mid, apply_criteria, member_stats, price_on_or_before

log = logging.getLogger(__name__)

COMPARISONS = {
    "A": ("active", "beat", "Beat SPY at formation (trade-date prices) vs trailed — active members"),
    "B": ("active", "beat_delay", "Beat SPY at formation (delay-adjusted) vs trailed — active members"),
    "C": ("other_ok", "gate_ok", "Kept by the beat-SPY gate vs dropped by it — members passing every other criterion"),
}


def forward_excess(close: pd.Series, spy: pd.Series, filed: date, horizon: int) -> tuple[float | None, str]:
    """Excess return vs SPY from the close of the first session AFTER `filed` to `horizon` sessions later."""
    i = int(close.index.searchsorted(pd.Timestamp(filed), side="right"))
    if i >= len(close):
        return None, "no price after filing"
    j = i + horizon
    if j >= len(close):
        return None, "horizon incomplete"
    s0, s1 = price_on_or_before(spy, close.index[i]), price_on_or_before(spy, close.index[j])
    if not s0 or not s1 or close.index[j] > spy.index[-1]:
        return None, "benchmark missing"
    return float(close.iloc[j] / close.iloc[i] - 1) - float(s1 / s0 - 1), "ok"


def formation_dates(start: date, end: date, window_days: int, step_days: int) -> list[date]:
    out, f = [], start + timedelta(days=window_days)
    while f < end:
        out.append(f)
        f += timedelta(days=step_days)
    return out


def screen_at(tx: pd.DataFrame, closes: dict[str, pd.Series], spy: pd.Series, F: date, S: dict) -> pd.DataFrame:
    """Every member as the screen saw them on F, using only trades filed by F."""
    known = tx[tx["bioguide"].notna() & tx["filing_date"].notna() & (tx["filing_date"] <= F)]
    rows = []
    for bg, g in known.groupby("bioguide"):
        st = member_stats(g, closes, spy, F, S, long_window=False)
        if st["n_trades"] == 0:
            continue
        ex, exd = st["excess_1y"], st["excess_1y_delay"]
        rows.append({"bioguide": bg, "formation": F, "n_trades": st["n_trades"], "n_buys": st["n_buys"],
                     "excess_1y": ex, "excess_1y_delay": exd,
                     "active": st["n_trades"] >= int(S["min_trades"]) and pd.notna(ex),
                     "beat": bool(pd.notna(ex) and ex > 0), "beat_delay": bool(pd.notna(exd) and exd > 0),
                     "gate_ok": apply_criteria(st, S, "gate")[0], "other_ok": apply_criteria(st, S, "score")[0]})
    return pd.DataFrame(rows)


def walk_forward(tx: pd.DataFrame, closes: dict[str, pd.Series], spy: pd.Series, S: dict, dates: list[date],
                 step_days: int, horizon: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (form, obs): one row per member per formation date, and one row per followed purchase."""
    forms, obs = [], []
    buys_all = tx[(tx["type"] == "P") & (tx["asset_type"] == "ST") & tx["ticker"].notna() & tx["bioguide"].notna()
                  & tx["filing_date"].notna()]
    for F in dates:
        form = screen_at(tx, closes, spy, F, S)
        if form.empty:
            continue
        forms.append(form)
        nxt = buys_all[(buys_all["filing_date"] > F) & (buys_all["filing_date"] <= F + timedelta(days=step_days))
                       & buys_all["bioguide"].isin(form["bioguide"])]
        fi = form.set_index("bioguide")
        for r in nxt.itertuples():
            close = closes.get(r.ticker)
            fx, status = (None, "unpriced ticker") if close is None else forward_excess(close, spy, r.filing_date, horizon)
            m = fi.loc[r.bioguide]
            obs.append({"formation": F, "bioguide": r.bioguide, "ticker": r.ticker, "trade_date": r.trade_date,
                        "filing_date": r.filing_date, "amount_mid": amount_mid(r.amount_low, r.amount_high),
                        "fwd_excess": fx, "status": status,
                        **{k: m[k] for k in ("active", "beat", "beat_delay", "gate_ok", "other_ok",
                                             "excess_1y", "excess_1y_delay")}})
    form = pd.concat(forms, ignore_index=True) if forms else pd.DataFrame()
    return form, pd.DataFrame(obs)


def compare(obs: pd.DataFrame, base: str, flag: str, samples: int, level: float, min_members: int,
            seed: int = 0) -> dict:
    """Buy-weighted mean forward excess for flag=True vs flag=False within `base`, with a member-cluster bootstrap
    CI of the difference. A member can sit on both sides (different quarters); resampling keeps all their rows."""
    d = obs[obs[base].astype(bool) & obs["fwd_excess"].notna()]
    out = {"base": base, "flag": flag}
    if d.empty:
        return {**out, "verdict": "no completed purchases", "n_buys_1": 0, "n_buys_0": 0, "n_members_1": 0, "n_members_0": 0}
    yes = d[flag].astype(bool)
    g = d.assign(_y=yes, _x=d["fwd_excess"].astype(float))
    per = g.groupby("bioguide").apply(lambda h: pd.Series({
        "s1": h.loc[h["_y"], "_x"].sum(), "c1": h["_y"].sum(), "s0": h.loc[~h["_y"], "_x"].sum(), "c0": (~h["_y"]).sum()}),
        include_groups=False)
    S1, C1, S0, C0 = (per[c].to_numpy(float) for c in ("s1", "c1", "s0", "c0"))
    out.update({"n_buys_1": int(C1.sum()), "n_buys_0": int(C0.sum()),
                "n_members_1": int((C1 > 0).sum()), "n_members_0": int((C0 > 0).sum()),
                "mean_1": S1.sum() / C1.sum() if C1.sum() else np.nan, "mean_0": S0.sum() / C0.sum() if C0.sum() else np.nan,
                "hit_1": float((g.loc[g["_y"], "_x"] > 0).mean()) if C1.sum() else np.nan,
                "hit_0": float((g.loc[~g["_y"], "_x"] > 0).mean()) if C0.sum() else np.nan})
    out["diff"] = out["mean_1"] - out["mean_0"]
    if out["n_members_1"] < min_members or out["n_members_0"] < min_members:
        return {**out, "ci_low": np.nan, "ci_high": np.nan,
                "verdict": f"too few members to judge ({out['n_members_1']} vs {out['n_members_0']}; need {min_members} each)"}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(per), size=(samples, len(per)))
    with np.errstate(invalid="ignore", divide="ignore"):
        diffs = S1[idx].sum(1) / C1[idx].sum(1) - S0[idx].sum(1) / C0[idx].sum(1)
    diffs = diffs[np.isfinite(diffs)]
    a = (1 - level) / 2
    lo, hi = float(np.quantile(diffs, a)), float(np.quantile(diffs, 1 - a))
    out.update({"ci_low": lo, "ci_high": hi})
    out["verdict"] = ("past outperformers' next purchases beat the others'" if lo > 0 else
                      "past outperformers' next purchases did WORSE than the others'" if hi < 0 else
                      "no detectable difference (the CI spans zero)")
    return out


def rank_correlation(obs: pd.DataFrame, samples: int, level: float, seed: int = 0) -> dict:
    """Spearman correlation, over member-quarters of active members, between the delay-adjusted excess at formation
    and the mean forward excess of that quarter's purchases. CI from a member-cluster bootstrap."""
    d = obs[obs["active"].astype(bool) & obs["fwd_excess"].notna() & obs["excess_1y_delay"].notna()]
    mq = d.groupby(["bioguide", "formation"]).agg(x=("excess_1y_delay", "first"), y=("fwd_excess", "mean")).reset_index()
    if len(mq) < 5:
        return {"n": len(mq), "rho": np.nan, "ci_low": np.nan, "ci_high": np.nan}

    def rho(f: pd.DataFrame) -> float:
        return float(np.corrcoef(f["x"].rank(), f["y"].rank())[0, 1]) if len(f) > 2 else np.nan

    members = mq["bioguide"].unique()
    by = {m: mq[mq["bioguide"] == m] for m in members}
    rng = np.random.default_rng(seed)
    boots = [rho(pd.concat([by[m] for m in rng.choice(members, len(members))])) for _ in range(samples)]
    boots = np.array([b for b in boots if np.isfinite(b)])
    a = (1 - level) / 2
    return {"n": len(mq), "members": len(members), "rho": rho(mq),
            "ci_low": float(np.quantile(boots, a)) if len(boots) else np.nan,
            "ci_high": float(np.quantile(boots, 1 - a)) if len(boots) else np.nan}


def by_quarter(obs: pd.DataFrame, base: str, flag: str) -> pd.DataFrame:
    """Per formation date: mean forward excess on each side (stability check: does the sign flip?)."""
    d = obs[obs[base].astype(bool) & obs["fwd_excess"].notna()]
    if d.empty:
        return pd.DataFrame()
    t = d.groupby(["formation", d[flag].astype(bool)])["fwd_excess"].agg(["mean", "size"]).unstack(flag)
    t.columns = [f"{a}_{'yes' if b else 'no'}" for a, b in t.columns]
    for c in ("mean_yes", "mean_no", "size_yes", "size_no"):
        if c not in t:
            t[c] = np.nan
    t["diff"] = t["mean_yes"] - t["mean_no"]
    return t


def run(cfg: Settings | None = None, years: int | None = None, price_provider=None, house=None, congress=None,
        senate=None, today: date | None = None) -> dict:
    from core.providers.congress import CongressProvider
    from core.providers.house_disclosures import HouseDisclosureProvider
    from core.providers.yfinance_provider import YFinanceProvider
    from scanners.political.scan import ingest

    cfg = cfg or default_settings()
    P = cfg.section("political")
    S, PS = P["screen"], P["persistence"]
    n_years = int(years or PS["years"])
    if n_years < 2:
        raise ValueError("persistence needs at least 2 filing years")
    today = today or date.today()
    yrs = [today.year - k for k in range(n_years)]
    prov = price_provider or YFinanceProvider(cfg)
    tx, coverage, members, unmatched, dupes = ingest(cfg, yrs, house or HouseDisclosureProvider(cfg),
                                                     congress or CongressProvider(cfg), senate)
    tickers = sorted(set(tx.loc[(tx["asset_type"] == "ST") & tx["ticker"].notna(), "ticker"]))
    bench = cfg.get("indicators.benchmark")
    hist = prov.history(tickers + [bench], "5y" if n_years <= 4 else "10y")
    if bench not in hist:
        raise RuntimeError("benchmark prices unavailable")
    closes = {t: df["close"] for t, df in hist.items()}
    spy = closes[bench]
    price_as_of = spy.index[-1].date()

    step, horizon = int(PS["step_days"]), int(PS["horizon_days"])
    dates = formation_dates(date(min(yrs), 1, 1), price_as_of, int(S["window_days"]), step)
    log.info("%d formation dates, %d transactions", len(dates), len(tx))
    form, obs = walk_forward(tx, closes, spy, S, dates, step, horizon)
    samples, level, mm = int(PS["bootstrap_samples"]), float(PS["ci_level"]), int(PS["min_members"])
    results = {k: {**compare(obs, b, f, samples, level, mm), "label": lab} for k, (b, f, lab) in COMPARISONS.items()} \
        if len(obs) else {}
    corr = rank_correlation(obs, min(samples, 2000), level) if len(obs) else {}
    quarters = by_quarter(obs, "active", "beat_delay") if len(obs) else pd.DataFrame()

    out = cfg.path_for("paths.reports_dir")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"political-persistence-{today.isoformat()}"
    if len(obs):
        obs.to_csv(out / f"{stem}-purchases.csv", index=False)
    md = out / f"{stem}.md"
    md.write_text(_report(results, corr, quarters, obs, form, dates, yrs, coverage, unmatched, dupes, price_as_of,
                          step, horizon, level, S), encoding="utf-8")
    return {"results": results, "corr": corr, "quarters": quarters, "obs": obs, "form": form, "dates": dates,
            "price_as_of": price_as_of, "path": md}


def _pct(x) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:+.1%}"


def _share(x) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:.0%}"


def _report(results, corr, quarters, obs, form, dates, yrs, coverage, unmatched, dupes, price_as_of, step, horizon,
            level, S) -> str:
    done = obs[obs["fwd_excess"].notna()] if len(obs) else obs
    status = obs["status"].value_counts().to_dict() if len(obs) else {}
    L = [f"# Political edge — does past performance persist? ({date.today().isoformat()})", "",
         f"_Research only. Walk-forward on public PTR disclosures; prices as of close {price_as_of}. "
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Read-only: no setting was changed._", "",
         "## Method", "",
         f"- Filing years {min(yrs)}–{max(yrs)}: {int(coverage['parsed'].sum())} of {len(coverage)} filings parsed; "
         f"{dupes} amended-report repeats removed.",
         f"- {len(dates)} formation dates, every {step} days from {dates[0] if dates else '—'} to {dates[-1] if dates else '—'}. "
         f"At each, members are screened on trades **filed** by that date ({S['window_days']}-day window, current thresholds).",
         f"- Followed: each stock purchase **filed** in the next {step} days; entry at the close of the first session after "
         f"filing; held {horizon} sessions; excess vs SPY over the same dates.",
         f"- Purchases followed: {len(obs)}; completed: **{len(done)}**; "
         + ", ".join(f"{k}: {v}" for k, v in status.items() if k != "ok"),
         f"- CIs: {level:.0%}, cluster bootstrap over members (a member's purchases move together).", "",
         "## Results", "",
         "| | Comparison | Purchases (yes / no) | Members (yes / no) | Mean excess yes | Mean excess no | Difference | "
         f"{level:.0%} CI | Hit rate yes / no | Verdict |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for k, r in results.items():
        ci = f"[{_pct(r.get('ci_low'))}, {_pct(r.get('ci_high'))}]"
        L.append(f"| {k} | {r['label']} | {r.get('n_buys_1', 0)} / {r.get('n_buys_0', 0)} | "
                 f"{r.get('n_members_1', 0)} / {r.get('n_members_0', 0)} | {_pct(r.get('mean_1'))} | {_pct(r.get('mean_0'))} | "
                 f"{_pct(r.get('diff'))} | {ci} | {_share(r.get('hit_1'))} / {_share(r.get('hit_0'))} | {r['verdict']} |")
    if corr:
        L += ["", f"Rank correlation (active members, delay-adjusted formation excess vs that quarter's mean forward "
                  f"excess): rho = {corr['rho']:+.2f} over {corr['n']} member-quarters "
                  + (f"({corr.get('members', 0)} members), {level:.0%} CI [{corr['ci_low']:+.2f}, {corr['ci_high']:+.2f}]."
                     if pd.notna(corr.get("rho", np.nan)) else "(too few to compute).")]
    if len(quarters):
        L += ["", "### By formation date (comparison B)", "",
              "| Formation | Purchases beat / trailed | Mean beat | Mean trailed | Difference |", "|---|---|---|---|---|"]
        for f, r in quarters.iterrows():
            L.append(f"| {f} | {int(np.nan_to_num(r['size_yes']))} / {int(np.nan_to_num(r['size_no']))} | "
                     f"{_pct(r['mean_yes'])} | {_pct(r['mean_no'])} | {_pct(r['diff'])} |")
    L += ["", "## Limits", "",
          "- Current members only: members who left Congress are not in the members file and are excluded "
          f"({len(unmatched)} unmatched filers), a survivorship bias of unknown sign.",
          "- Copy-trade performance uses amount-range midpoints; scanned paper filings are unread.",
          "- Purchases are equal-weighted; all members' purchases in a quarter share one market, so quarters are not "
          "independent. The member-cluster bootstrap handles same-member correlation, not market-wide shocks.",
          "- The forward test is the copy-trade, not the political-edge plan (no stop, no target, no committee filter); "
          "it isolates whether the member-performance signal carries information.",
          "- A CI that spans zero means 'no detectable difference with this much data', not proof of no effect."]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Political edge: does member outperformance persist? (read-only)")
    ap.add_argument("--years", type=int, default=None, help="filing years to load (default political.persistence.years)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    res = run(years=a.years)
    for k, r in res["results"].items():
        ci = f"[{_pct(r.get('ci_low'))}, {_pct(r.get('ci_high'))}]"
        print(f"{k}: diff {_pct(r.get('diff'))} {ci}  ({r.get('n_buys_1', 0)}/{r.get('n_buys_0', 0)} purchases, "
              f"{r.get('n_members_1', 0)}/{r.get('n_members_0', 0)} members) -> {r['verdict']}")
    c = res["corr"]
    if c:
        print(f"rank corr: {c.get('rho', np.nan):+.2f} [{c.get('ci_low', np.nan):+.2f}, {c.get('ci_high', np.nan):+.2f}] n={c.get('n')}")
    print(f"-> {res['path']}")


if __name__ == "__main__":
    main()
