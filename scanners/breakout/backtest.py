"""breakout-scanner backtest: replay the TECHNICAL scan historically.

    uv run python -m scanners.breakout.backtest [--source list|nasdaqtrader] [--limit N] [--period 5y]

Uses the exact same feature / gate / plan code as the live scan (scanners/breakout/setups.py).
Reports hit rate, average gain vs loss, and expectancy, against SPY over the same holding periods.

Known biases (also printed in the report):
- Survivorship: the ticker list is TODAY's liquid universe; delisted losers are missing.
- Fundamentals are not applied (no point-in-time data), so this tests the technical layer only.
- Earnings-date exclusion is not applied.
- No capital constraint: every signal is taken independently (no max positions / sector caps).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import asdict, dataclass
from datetime import date

import numpy as np
import pandas as pd

from core import indicators as ind
from core.config import Settings, settings as default_settings
from core.universe import build_universe
from scanners.breakout.setups import Plan, compute_features, make_plan, passes_gates, technical_score

log = logging.getLogger(__name__)
WARMUP = 252  # bars before the first signal (SMA200 + 52-week RS high need history)


@dataclass
class Trade:
    ticker: str
    setup: str
    signal_date: date
    fill_date: date
    exit_date: date
    fill: float
    stop: float
    target: float
    exit: float
    exit_reason: str          # stop | target | time | open_at_end
    ret: float                # after slippage
    r_multiple: float
    bars_held: int
    spy_ret: float | None = None


# Exit / entry rules. Defaults reproduce the original fixed-target, fixed-stop plan.
DEFAULT_EXITS = {
    "target_mode": "fixed",          # fixed = exit at the plan target; none = no target, let winners run
    "breakeven_at_r": None,          # after the high reaches +X R, raise the stop to the entry price
    "trail_atr_mult": None,          # trail the stop at (highest close since entry - X * ATR)
    "trail_low_days": None,          # trail the stop at the lowest low of the last N bars
    "ma_exit": None,                 # "ema21" | "sma50": exit next open after a close below that average
    "pre_breakout_entry": "buy_stop",  # buy_stop | confirm_close (wait for a close above pivot on volume)
    "confirm_rvol": 1.5,             # volume needed for confirm_close
}


def simulate_trade(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int, plan: Plan,
                   pending_days: int, horizon: int, slippage: float = 0.0,
                   same_bar_rule: str = "stop_first", exits: dict | None = None,
                   aux: dict[str, np.ndarray] | None = None) -> tuple[int, int, float, float, str] | None:
    """Simulate one plan signalled on bar i (decided at bar i's close). Returns
    (fill_idx, exit_idx, fill_px, exit_px, reason) or None if never filled.

    Entry: breakout/pullback -> next bar's open if it's inside (stop, entry_high];
           pre_breakout, buy_stop -> buy-stop at entry_low for `pending_days` bars, fill max(open, entry_low);
           skipped if a bar opens above entry_high (gapped through the zone), cancelled if price undercuts
           `cancel_below` (the base low) before triggering. (Trading below the stop while still pending is
           normal for a buy-stop below the pivot and does not cancel it.)
           pre_breakout, confirm_close -> wait for a CLOSE above the pivot with rvol >= confirm_rvol, then
           buy the next open if it's inside (stop, entry_high].
    Exit:  stop (fill at min(open, stop) after the fill bar), target (max(open, target)), or time stop
           at the close of the `horizon`-th bar held. If both are hit in one bar, `same_bar_rule` decides.
           On an intrabar buy-stop fill bar (open below the trigger) the day's low can't be ordered vs the
           fill, so the stop counts only if the bar CLOSES below it (exit at the close).
           Optional (see DEFAULT_EXITS): no target, breakeven stop, ATR / N-day-low trailing stops, MA exit.
           Trailing levels are computed at a bar's close and apply from the NEXT bar (no look-ahead).
    """
    ex = {**DEFAULT_EXITS, **(exits or {})}
    aux = aux or {}
    n = len(c)
    fill_idx = fill_px = None
    intrabar_fill = False
    if plan.setup in ("breakout", "pullback"):
        j = i + 1
        if j < n and plan.stop < o[j] <= plan.entry_high:
            fill_idx, fill_px = j, o[j]
    elif ex["pre_breakout_entry"] == "confirm_close":
        rv = aux["rvol"]
        for j in range(i + 1, min(i + 1 + pending_days, n)):
            if plan.cancel_below is not None and l[j] <= plan.cancel_below:
                return None
            if c[j] > plan.entry_low and rv[j] >= float(ex["confirm_rvol"]):
                if j + 1 < n and plan.stop < o[j + 1] <= plan.entry_high:
                    fill_idx, fill_px = j + 1, o[j + 1]
                break
    else:
        for j in range(i + 1, min(i + 1 + pending_days, n)):
            if o[j] > plan.entry_high:
                return None
            if h[j] >= plan.entry_low:
                fill_idx, fill_px = j, max(o[j], plan.entry_low)
                intrabar_fill = o[j] < plan.entry_low
                break
            if plan.cancel_below is not None and l[j] <= plan.cancel_below:
                return None
    if fill_idx is None:
        return None
    raw_fill = fill_px
    fill_px *= 1 + slippage

    target = plan.target if ex["target_mode"] == "fixed" else float("inf")
    stop = plan.stop
    risk0 = raw_fill - plan.stop
    peak = raw_fill

    def stop_reason() -> str:
        return "stop" if stop <= plan.stop else "trail_stop"

    last = min(fill_idx + horizon - 1, n - 1)
    for k in range(fill_idx, last + 1):
        stop_hit, tgt_hit = l[k] <= stop, h[k] >= target
        if k == fill_idx and intrabar_fill:
            stop_hit = c[k] < stop
            if stop_hit and not tgt_hit:
                return fill_idx, k, fill_px, c[k] * (1 - slippage), stop_reason()
        if k == fill_idx:
            stop_px, tgt_px = stop, target        # intrabar after the fill: no gap through
        else:
            stop_px, tgt_px = min(o[k], stop), max(o[k], target)
        if stop_hit and tgt_hit:
            if same_bar_rule == "stop_first":
                return fill_idx, k, fill_px, stop_px * (1 - slippage), stop_reason()
            return fill_idx, k, fill_px, tgt_px * (1 - slippage), "target"
        if stop_hit:
            return fill_idx, k, fill_px, stop_px * (1 - slippage), stop_reason()
        if tgt_hit:
            return fill_idx, k, fill_px, tgt_px * (1 - slippage), "target"
        if k == last:
            break
        # End of bar k: close-based exit and stop updates take effect from bar k+1.
        if ex["ma_exit"] and c[k] < aux[ex["ma_exit"]][k]:
            return fill_idx, k + 1, fill_px, o[k + 1] * (1 - slippage), "ma_exit"
        if ex["breakeven_at_r"] is not None and h[k] >= raw_fill + float(ex["breakeven_at_r"]) * risk0:
            stop = max(stop, raw_fill)
        peak = max(peak, c[k])
        if ex["trail_atr_mult"] is not None:
            stop = max(stop, peak - float(ex["trail_atr_mult"]) * aux["atr"][k])
        if ex["trail_low_days"] is not None:
            stop = max(stop, aux[f"low{int(ex['trail_low_days'])}"][k])
    reason = "time" if last == fill_idx + horizon - 1 else "open_at_end"
    return fill_idx, last, fill_px, c[last] * (1 - slippage), reason


def summarize(trades: pd.DataFrame) -> pd.DataFrame:
    """Per-setup and overall stats. Returns are fractions."""
    if trades.empty:
        return pd.DataFrame()

    def stats(t: pd.DataFrame) -> dict:
        wins, losses = t[t["ret"] > 0], t[t["ret"] <= 0]
        spy = t["spy_ret"].dropna()
        return {
            "trades": len(t),
            "hit_rate": len(wins) / len(t),
            "avg_gain": wins["ret"].mean() if len(wins) else np.nan,
            "avg_loss": losses["ret"].mean() if len(losses) else np.nan,
            "expectancy_pct": t["ret"].mean(),
            "expectancy_r": t["r_multiple"].mean(),
            "avg_spy_same_period": spy.mean() if len(spy) else np.nan,
            "avg_excess_vs_spy": (t["ret"] - t["spy_ret"]).mean() if len(spy) else np.nan,
            "avg_bars_held": t["bars_held"].mean(),
            "stops": int(t["exit_reason"].isin(["stop", "trail_stop"]).sum()),
            "targets": int((t["exit_reason"] == "target").sum()),
            "time_exits": int((t["exit_reason"] == "time").sum()),
        }

    rows = {"ALL": stats(trades)}
    for setup, grp in trades.groupby("setup"):
        rows[setup] = stats(grp)
    return pd.DataFrame(rows).T


def prepare(histories: dict[str, pd.DataFrame], bench: pd.DataFrame, cfg: Settings):
    """Compute features once (slow part) so several exit variants can be simulated on them."""
    b = cfg.section("breakout")
    atr_p, rs_lb = int(cfg.get("indicators.atr_period")), int(cfg.get("indicators.rs_lookback_days"))
    horizon = int(b["plan"]["horizon_days"])
    feats = {t: compute_features(df, bench["close"], b, atr_p, rs_lb)
             for t, df in histories.items() if len(df) > WARMUP + horizon}
    if not feats:
        return {}, pd.DataFrame()
    # Cross-sectional RS percentile per date, among tickers with data that day (no look-ahead).
    rs = pd.DataFrame({t: f["rs_ret"] for t, f in feats.items()})
    return feats, rs.rank(axis=1, pct=True) * 100


def generate_trades(feats: dict[str, pd.DataFrame], rs_pct: pd.DataFrame, bench: pd.DataFrame, cfg: Settings,
                    exits: dict | None = None, horizon: int | None = None) -> pd.DataFrame:
    b = cfg.section("breakout")
    bt = b["backtest"]
    ex = {**DEFAULT_EXITS, **(cfg.get("breakout.exits", None) or {}), **(exits or {})}
    step, pending = int(bt["signal_step_days"]), int(bt["pending_days"])
    horizon = int(horizon or b["plan"]["horizon_days"])
    slip = float(bt.get("slippage_pct", 0.0))
    min_score = float(b["scoring"]["min_score"])
    spy_close = bench["close"]

    trades: list[Trade] = []
    for t, f in feats.items():
        o, h, l, c = (f[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        aux = {k: f[k].to_numpy(float) for k in ("atr", "ema21", "sma50", "rvol")}
        if ex["trail_low_days"] is not None:
            d = int(ex["trail_low_days"])
            aux[f"low{d}"] = f["low"].rolling(d, min_periods=1).min().to_numpy(float)
        idx = f.index
        pct_col = rs_pct[t].reindex(idx).to_numpy(float)
        busy_until = -1
        for i in range(WARMUP, len(f) - 1, step):
            if i <= busy_until:
                continue
            row = f.iloc[i]
            if passes_gates(row, pct_col[i], b):
                continue
            plan, _ = make_plan(row, b)
            if plan is None or technical_score(row, pct_col[i], b) < min_score:
                continue
            res = simulate_trade(o, h, l, c, i, plan, pending, horizon, slip, bt["same_bar_rule"], ex, aux)
            if res is None:
                continue
            fi, xi, fpx, xpx, reason = res
            busy_until = xi
            risk = fpx - plan.stop
            spy_ret = None
            d0, d1 = idx[fi - 1], idx[xi]
            if d0 in spy_close.index and d1 in spy_close.index:
                spy_ret = float(spy_close[d1] / spy_close[d0] - 1)
            trades.append(Trade(t, plan.setup, idx[i].date(), idx[fi].date(), idx[xi].date(), fpx, plan.stop,
                                plan.target, xpx, reason, xpx / fpx - 1,
                                (xpx - fpx) / risk if risk > 0 else np.nan, xi - fi + 1, spy_ret))
    return pd.DataFrame([asdict(tr) for tr in trades])


def run_backtest(histories: dict[str, pd.DataFrame], bench: pd.DataFrame, cfg: Settings) -> pd.DataFrame:
    feats, rs_pct = prepare(histories, bench, cfg)
    if not feats:
        return pd.DataFrame()
    return generate_trades(feats, rs_pct, bench, cfg)


def write_report(trades: pd.DataFrame, summary: pd.DataFrame, cfg: Settings, n_tickers: int,
                 period: str, first: date | None, last: date | None) -> tuple:
    out = cfg.path_for("paths.reports_dir")
    out.mkdir(parents=True, exist_ok=True)
    stem = f"backtest-breakout-{date.today().isoformat()}"
    trades.to_csv(out / f"{stem}-trades.csv", index=False)
    pct_cols = ["hit_rate", "avg_gain", "avg_loss", "expectancy_pct", "avg_spy_same_period", "avg_excess_vs_spy"]
    view = summary.copy()
    for col in pct_cols:
        if col in view:
            view[col] = view[col].map(lambda v: "—" if pd.isna(v) else f"{v:+.2%}" if col != "hit_rate" else f"{v:.1%}")
    if "expectancy_r" in view:
        view["expectancy_r"] = view["expectancy_r"].map(lambda v: "—" if pd.isna(v) else f"{v:+.2f}R")
    if "avg_bars_held" in view:
        view["avg_bars_held"] = view["avg_bars_held"].map(lambda v: f"{v:.1f}")
    header = "| setup | " + " | ".join(view.columns) + " |"
    sep = "|" + "---|" * (len(view.columns) + 1)
    rows = [f"| {k} | " + " | ".join(str(x) for x in r) + " |" for k, r in view.iterrows()]
    bt = cfg.section("breakout.backtest")
    md = [
        f"# Breakout backtest — {date.today().isoformat()}",
        "",
        f"_Research only. {n_tickers} tickers, {period} of daily data ({first} to {last}), "
        f"signals every {bt['signal_step_days']} bars, slippage {float(bt.get('slippage_pct', 0)):.2%} per side, "
        f"same-bar rule: {bt['same_bar_rule']}._",
        "",
        header, sep, *rows,
        "",
        "## How to read this",
        "- **expectancy_pct** = average return per trade; **expectancy_r** = average result in units of initial risk.",
        "- **avg_excess_vs_spy** = trade return minus SPY's return over the same dates.",
        "",
        "## Biases — read before trusting these numbers",
        "- **Survivorship:** tickers are today's liquid universe. Stocks that broke down and delisted are missing, "
        "which flatters results.",
        "- **Technical only:** fundamentals and the earnings-date exclusion are not applied (no point-in-time data).",
        "- **No portfolio constraints:** every signal is taken; overlapping trades are not capped by positions or sector.",
        "- Prices are split/dividend-adjusted closes from Yahoo; intraday fill order is assumed, not observed.",
    ]
    (out / f"{stem}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return out / f"{stem}.md", out / f"{stem}-trades.csv"


def main(argv: list[str] | None = None) -> None:
    from core.providers.yfinance_provider import YFinanceProvider

    ap = argparse.ArgumentParser(description="Backtest the breakout scanner (technical layer).")
    ap.add_argument("--source", choices=["list", "nasdaqtrader"])
    ap.add_argument("--limit", type=int, help="candidate limit for the universe step")
    ap.add_argument("--period", help="history period, e.g. 5y (default from settings)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    cfg = default_settings()
    bt = cfg.section("breakout.backtest")
    period = args.period or bt["period"]
    prov = YFinanceProvider(cfg)
    uni = build_universe(cfg, source=args.source, limit=args.limit, price_provider=prov)
    tickers = uni.tickers[: int(bt["max_tickers"])]
    log.info("backtesting %d tickers over %s", len(tickers), period)
    histories = prov.history(tickers, period)
    bench_sym = cfg.get("indicators.benchmark")
    bench = prov.history([bench_sym], period)[bench_sym]

    trades = run_backtest(histories, bench, cfg)
    if trades.empty:
        print("No trades generated.")
        return
    summary = summarize(trades)
    first = min(df.index[0] for df in histories.values()).date()
    last = max(df.index[-1] for df in histories.values()).date()
    md, csv = write_report(trades, summary, cfg, len(histories), period, first, last)
    pd.set_option("display.width", 200)
    print(summary.to_string(float_format=lambda v: f"{v:.4f}"))
    print(f"\nReport: {md}\nTrades: {csv}")


if __name__ == "__main__":
    main()
