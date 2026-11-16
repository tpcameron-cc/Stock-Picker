"""Exit / entry experiments for the breakout scanner. Read-only: changes nothing in settings.yaml.

    uv run python -m scanners.breakout.experiments [--limit 3000]

Same signals for every variant (same features, gates, plans); only entry/exit rules differ.
Robustness checks against overfitting:
  - time split: expectancy in the first vs second half of the test period
  - holdout: the same variants on a second, disjoint set of tickers
Writes reports/experiments-breakout-YYYY-MM-DD.md.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date

import numpy as np
import pandas as pd

from core.config import settings as default_settings
from core.universe import build_universe
from scanners.breakout.backtest import generate_trades, prepare

log = logging.getLogger(__name__)

# name -> (exit overrides, horizon override, description)
VARIANTS: dict[str, tuple[dict, int | None, str]] = {
    "baseline":        ({}, None, "fixed target, fixed stop, 30-day time stop (current settings)"),
    "time_15":         ({}, 15, "baseline with a 15-day time stop"),
    "time_10":         ({}, 10, "baseline with a 10-day time stop"),
    "breakeven_1R":    ({"breakeven_at_r": 1.0}, None, "baseline + stop to entry after +1R"),
    "fixed_trail_3atr": ({"trail_atr_mult": 3.0}, None, "fixed target + 3 ATR trailing stop"),
    "run_trail_3atr":  ({"target_mode": "none", "trail_atr_mult": 3.0}, 60, "no target, 3 ATR trail, 60d max"),
    "run_trail_2atr":  ({"target_mode": "none", "trail_atr_mult": 2.0}, 60, "no target, 2 ATR trail, 60d max"),
    "run_low10":       ({"target_mode": "none", "trail_low_days": 10}, 60, "no target, trail 10-day low, 60d max"),
    "run_ema21":       ({"target_mode": "none", "ma_exit": "ema21"}, 60, "no target, exit on close < EMA21, 60d max"),
    "confirm":         ({"pre_breakout_entry": "confirm_close"}, None,
                        "pre-breakouts wait for a close above pivot on 1.5x volume; baseline exits"),
    "confirm_run_ema21": ({"pre_breakout_entry": "confirm_close", "target_mode": "none", "ma_exit": "ema21"}, 60,
                          "confirm entry + no target + EMA21 exit"),
    "confirm_run_3atr": ({"pre_breakout_entry": "confirm_close", "target_mode": "none", "trail_atr_mult": 3.0}, 60,
                         "confirm entry + no target + 3 ATR trail"),
}


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    return {
        "trades": len(t),
        "hit_rate": (t["ret"] > 0).mean(),
        "exp_r": t["r_multiple"].mean(),
        "exp_pct": t["ret"].mean(),
        "excess_vs_spy": (t["ret"] - t["spy_ret"]).mean(),
        "avg_bars": t["bars_held"].mean(),
        "median_r": t["r_multiple"].median(),
        "best_r": t["r_multiple"].max(),
        # Share of total R from the top 5% of trades: high = results hinge on a few outliers.
        "top5pct_share": _top_share(t["r_multiple"]),
    }


def _top_share(r: pd.Series) -> float:
    pos = r[r > 0].sort_values(ascending=False)
    if pos.sum() <= 0:
        return np.nan
    k = max(1, int(np.ceil(len(r) * 0.05)))
    return float(pos.head(k).sum() / pos.sum())


def run_variants(feats, rs_pct, bench, cfg, split: pd.Timestamp) -> tuple[pd.DataFrame, dict]:
    rows, trades_by = {}, {}
    for name, (ex, horizon, _) in VARIANTS.items():
        t = generate_trades(feats, rs_pct, bench, cfg, exits=ex, horizon=horizon)
        trades_by[name] = t
        s = stats(t)
        if not t.empty:
            fd = pd.to_datetime(t["fill_date"])
            s["exp_r_1st_half"] = t.loc[fd < split, "r_multiple"].mean()
            s["exp_r_2nd_half"] = t.loc[fd >= split, "r_multiple"].mean()
        rows[name] = s
        log.info("%-18s trades %4d  expR %+.3f", name, s.get("trades", 0), s.get("exp_r", np.nan))
    return pd.DataFrame(rows).T, trades_by


def _fmt(df: pd.DataFrame) -> pd.DataFrame:
    v = df.copy()
    for c in ("hit_rate", "exp_pct", "excess_vs_spy", "top5pct_share"):
        if c in v:
            v[c] = v[c].map(lambda x: "—" if pd.isna(x) else f"{x:.1%}" if c in ("hit_rate", "top5pct_share") else f"{x:+.2%}")
    for c in ("exp_r", "median_r", "best_r", "exp_r_1st_half", "exp_r_2nd_half", "holdout_exp_r"):
        if c in v:
            v[c] = v[c].map(lambda x: "—" if pd.isna(x) else f"{x:+.2f}")
    for c in ("trades", "holdout_trades"):
        if c in v:
            v[c] = v[c].map(lambda x: "—" if pd.isna(x) else f"{int(x)}")
    if "avg_bars" in v:
        v["avg_bars"] = v["avg_bars"].map(lambda x: f"{x:.1f}")
    return v


def md_table(df: pd.DataFrame, index_name: str) -> list[str]:
    cols = list(df.columns)
    out = ["| " + index_name + " | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 1)]
    out += [f"| {k} | " + " | ".join(str(x) for x in r) + " |" for k, r in df.iterrows()]
    return out


def main(argv: list[str] | None = None) -> None:
    from core.providers.yfinance_provider import YFinanceProvider

    ap = argparse.ArgumentParser(description="Breakout exit experiments (read-only).")
    ap.add_argument("--limit", type=int, default=3000, help="candidate limit for the universe step")
    ap.add_argument("--per-set", type=int, default=300, help="tickers in the main set and in the holdout set")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = default_settings()
    period = cfg.get("breakout.backtest.period")
    prov = YFinanceProvider(cfg)
    uni = build_universe(cfg, limit=args.limit, price_provider=prov)
    tickers = uni.tickers
    main_set, hold_set = tickers[: args.per_set], tickers[args.per_set : 2 * args.per_set]
    log.info("main set %d tickers, holdout %d tickers, %s", len(main_set), len(hold_set), period)
    bench_sym = cfg.get("indicators.benchmark")
    bench = prov.history([bench_sym], period)[bench_sym]

    feats, rs_pct = prepare(prov.history(main_set, period), bench, cfg)
    first = min(f.index[252] for f in feats.values() if len(f) > 252)
    last = max(f.index[-1] for f in feats.values())
    split = first + (last - first) / 2
    main_tbl, main_trades = run_variants(feats, rs_pct, bench, cfg, split)

    hold_tbl = pd.DataFrame()
    if hold_set:
        h_feats, h_rs = prepare(prov.history(hold_set, period), bench, cfg)
        hold_tbl, _ = run_variants(h_feats, h_rs, bench, cfg, split)
        main_tbl["holdout_trades"] = hold_tbl["trades"]
        main_tbl["holdout_exp_r"] = hold_tbl["exp_r"]

    # Per-setup breakdown for every variant (main set)
    per_setup = {}
    for name, t in main_trades.items():
        if not t.empty:
            per_setup[name] = t.groupby("setup")["r_multiple"].mean()
    setup_tbl = pd.DataFrame(per_setup).T

    cols = ["trades", "hit_rate", "exp_r", "exp_pct", "excess_vs_spy", "avg_bars", "median_r", "top5pct_share",
            "exp_r_1st_half", "exp_r_2nd_half", "holdout_trades", "holdout_exp_r"]
    main_tbl = main_tbl[[c for c in cols if c in main_tbl]]

    out = cfg.path_for("paths.reports_dir") / f"experiments-breakout-{date.today().isoformat()}.md"
    md = [
        f"# Breakout exit experiments — {date.today().isoformat()}",
        "",
        f"_Research only. Main set {len(feats)} tickers, holdout {len(hold_set)} different tickers; {period} daily data, "
        f"signals from {first.date()} to {last.date()}, time split at {split.date()}. "
        f"Slippage {float(cfg.get('breakout.backtest.slippage_pct')):.2%}/side. Technical layer only (no fundamentals)._",
        "",
        "## Variants", "",
        *[f"- **{k}**: {v[2]}" for k, v in VARIANTS.items()],
        "",
        "## Results (main set; holdout columns are the other ticker set)", "",
        *md_table(_fmt(main_tbl), "variant"),
        "",
        "## Expectancy (R) by setup, main set", "",
        *md_table(setup_tbl.map(lambda x: "—" if pd.isna(x) else f"{x:+.2f}"), "variant"),
        "",
        "## Caveats",
        f"- {len(VARIANTS)} variants were tried. The best of many will look better than it really is "
        "(multiple testing); trust a variant only if it holds in BOTH halves AND the holdout.",
        "- **top5pct_share** = share of winning R from the best 5% of trades; high means results hinge on a few outliers.",
        "- Survivorship bias (today's tickers), no fundamentals, no portfolio limits. See the backtest report.",
    ]
    out.write_text("\n".join(md) + "\n", encoding="utf-8")
    pd.set_option("display.width", 250)
    print(_fmt(main_tbl).to_string())
    print()
    print(setup_tbl.round(2).to_string())
    print(f"\nReport: {out}")


if __name__ == "__main__":
    main()
