---
name: breakout-scanner
description: Scan liquid US stocks for technical breakout setups (tight bases, VCP, flat base, cup-with-handle, breakouts on volume, pullbacks to rising averages) in RS leaders, filtered by fundamentals; or backtest the scan. Use when the user asks for breakouts, bases, technical setups, leaders near highs, or how the breakout scanner has performed historically. Research only, never places orders.
---

# breakout-scanner

Finds market leaders (50>150>200-day MA stack, relative strength in the top 20% vs SPY, near 52-week
highs) that are set up to break out, then checks that fundamentals support the move. Output is a list of
`Signal`s with pivot, entry zone, stop, measured-move target and distance to pivot. All logic lives in
`scanners/breakout/` and `core/`; thresholds are in `config/settings.yaml` under `breakout:`.

## When to use
- "Run the breakout scan" / "find breakouts" / "what's basing near highs?"
- As one of the four scanners run by `run-all-scans`.
- "How has the breakout scanner done historically?" → run the backtest.

## Steps
1. Run the scan from the project root (takes ~1–2 min for 600 stocks, ~15 min cold for the full universe; cached 12h):
   ```bash
   uv run python -m scanners.breakout.scan                 # full universe
   uv run python -m scanners.breakout.scan --limit 600     # quick partial run
   uv run python -m scanners.breakout.scan --no-fundamentals   # technical only (all ideas low confidence)
   ```
2. Read `reports/breakout-YYYY-MM-DD.md` (ideas, deferred, rejected) and `.json` (Signals).
3. Present the ideas to the user: setup, pivot, entry zone, stop, target, R:R, confidence, the thesis, and the
   sources with their as-of dates. Mention ideas deferred to `catalyst-plays` (earnings within 5 trading days)
   and summarize the main rejection reasons.
4. Be explicit about gaps: "fundamentals unverified" / "unavailable" flags mean lower confidence, not a pass.
   R:R below 2:1 is shown but `risk-and-sizing` will reject it.
5. Backtest when asked (or after threshold changes):
   ```bash
   uv run python -m scanners.breakout.backtest --limit 1500     # ~300 tickers, 5y
   ```
   Report hit rate, avg gain vs loss, expectancy (% and R) and excess vs SPY per setup, and always restate
   the biases section (survivorship, technical-only, no portfolio constraints).

## Output
- `reports/breakout-YYYY-MM-DD.json`: list of `core.signals.Signal` (skill = `breakout-scanner`), with
  `metrics`: setup (`breakout` | `pre_breakout` | `pullback`), pivot, distance_to_pivot, rs_percentile, rvol,
  base_depth, atr, target_basis, technical_score, plus fundamental metrics.
- `reports/breakout-YYYY-MM-DD.md`: readable summary with ideas, deferred and rejected tables.
- `reports/breakout-YYYY-MM-DD-rejected.csv`: ticker, stage (plan | fundamentals | score | shortlist | earnings), reason.
- Backtest: `reports/backtest-breakout-YYYY-MM-DD.md` and `-trades.csv`.

## Rules
- Never place or suggest placing orders through any tool. Plans are research.
- Do not change thresholds in `settings.yaml` without the user's approval.
