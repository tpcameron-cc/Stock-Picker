# Breakout exit experiments — 2026-11-16

_Research only. Main set 290 tickers, holdout 300 different tickers; 5y daily data, signals from 2022-11-17 to 2026-11-13, time split at 2024-11-14. Slippage 0.10%/side. Technical layer only (no fundamentals)._

## Variants

- **baseline**: fixed target, fixed stop, 30-day time stop (current settings)
- **time_15**: baseline with a 15-day time stop
- **time_10**: baseline with a 10-day time stop
- **breakeven_1R**: baseline + stop to entry after +1R
- **fixed_trail_3atr**: fixed target + 3 ATR trailing stop
- **run_trail_3atr**: no target, 3 ATR trail, 60d max
- **run_trail_2atr**: no target, 2 ATR trail, 60d max
- **run_low10**: no target, trail 10-day low, 60d max
- **run_ema21**: no target, exit on close < EMA21, 60d max
- **confirm**: pre-breakouts wait for a close above pivot on 1.5x volume; baseline exits
- **confirm_run_ema21**: confirm entry + no target + EMA21 exit
- **confirm_run_3atr**: confirm entry + no target + 3 ATR trail

## Results (main set; holdout columns are the other ticker set)

| variant | trades | hit_rate | exp_r | exp_pct | excess_vs_spy | avg_bars | median_r | top5pct_share | exp_r_1st_half | exp_r_2nd_half | holdout_trades | holdout_exp_r |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 514 | 36.0% | -0.10 | -0.56% | -1.32% | 12.0 | -1.01 | 30.4% | +0.02 | -0.26 | 559 | +0.05 |
| time_15 | 538 | 40.7% | -0.13 | -0.84% | -1.33% | 8.9 | -1.01 | 30.9% | -0.04 | -0.25 | 581 | +0.05 |
| time_10 | 566 | 41.5% | -0.11 | -0.66% | -1.06% | 7.0 | -0.32 | 33.1% | -0.05 | -0.19 | 611 | +0.03 |
| breakeven_1R | 526 | 27.2% | -0.17 | -1.08% | -1.66% | 10.4 | -1.01 | 38.6% | -0.10 | -0.25 | 574 | +0.04 |
| fixed_trail_3atr | 523 | 34.0% | -0.14 | -0.79% | -1.41% | 10.8 | -0.89 | 33.5% | -0.05 | -0.26 | 565 | +0.02 |
| run_trail_3atr | 480 | 26.5% | -0.13 | -0.57% | -1.55% | 16.6 | -0.94 | 54.1% | +0.02 | -0.31 | 513 | +0.12 |
| run_trail_2atr | 529 | 29.1% | -0.12 | -0.79% | -1.33% | 10.9 | -0.57 | 51.3% | +0.01 | -0.28 | 556 | +0.04 |
| run_low10 | 523 | 27.9% | -0.12 | -0.87% | -1.69% | 12.3 | -0.59 | 54.5% | +0.01 | -0.29 | 549 | +0.12 |
| run_ema21 | 518 | 28.8% | -0.08 | -0.65% | -1.40% | 12.1 | -0.44 | 57.9% | +0.02 | -0.21 | 569 | +0.02 |
| confirm | 353 | 38.8% | -0.08 | -0.48% | -1.31% | 11.2 | -1.01 | 29.9% | +0.02 | -0.20 | 364 | +0.05 |
| confirm_run_ema21 | 366 | 28.1% | -0.13 | -1.03% | -1.69% | 10.7 | -0.41 | 58.0% | -0.12 | -0.15 | 380 | -0.04 |
| confirm_run_3atr | 338 | 26.9% | -0.14 | -0.70% | -1.71% | 16.4 | -0.91 | 55.6% | +0.00 | -0.30 | 343 | +0.12 |

## Expectancy (R) by setup, main set

| variant | breakout | pre_breakout | pullback |
|---|---|---|---|
| baseline | -0.25 | -0.15 | +0.01 |
| time_15 | -0.22 | -0.16 | -0.07 |
| time_10 | -0.12 | -0.11 | -0.11 |
| breakeven_1R | -0.29 | -0.26 | -0.01 |
| fixed_trail_3atr | -0.21 | -0.23 | -0.02 |
| run_trail_3atr | -0.27 | -0.17 | -0.02 |
| run_trail_2atr | -0.22 | -0.10 | -0.10 |
| run_low10 | -0.31 | -0.11 | -0.06 |
| run_ema21 | -0.36 | -0.05 | -0.01 |
| confirm | -0.28 | -0.04 | -0.01 |
| confirm_run_ema21 | -0.35 | -0.19 | -0.03 |
| confirm_run_3atr | -0.29 | -0.27 | -0.03 |

## Caveats
- 12 variants were tried. The best of many will look better than it really is (multiple testing); trust a variant only if it holds in BOTH halves AND the holdout.
- **top5pct_share** = share of winning R from the best 5% of trades; high means results hinge on a few outliers.
- Survivorship bias (today's tickers), no fundamentals, no portfolio limits. See the backtest report.

## Findings (analysis 2026-11-16)

- **No exit or entry variant is robustly better than the baseline.** On the main set every variant is negative
  (-0.08R to -0.17R); on the holdout every variant except one is positive (+0.02R to +0.12R). The variants that look best
  on the holdout (`run_trail_3atr`, `run_low10`, +0.12R) are *worse* than baseline on the main set.
- **The two ticker sets disagree on almost every pattern.** Breakout setup: -0.25R main vs +0.14R holdout. Second half:
  -0.26R main vs +0.13R holdout. Market regime (SPY vs its 200-day): the sign flips between sets too.
  When patterns flip between samples, they are noise, not signal.
- **Bootstrap 90% confidence intervals for baseline mean R:** main [-0.20, -0.00], holdout [-0.06, +0.16].
  Pooled, the technical layer is roughly breakeven (about -0.02R/trade over ~1,070 trades), before fundamentals.
- **Conclusion:** exits are not the lever. The differences between variants are smaller than the sampling noise, so
  choosing one would be curve-fitting. Keep the current exits; no settings were changed.
