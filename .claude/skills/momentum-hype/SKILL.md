---
name: momentum-hype
description: Find stocks where retail attention is accelerating (Reddit/4chan mention velocity, StockTwits trending, r/wallstreetbets comments, news headline velocity, unusual call volume) AND price confirms it (above rising 10/20-day averages, 2x volume, holding gains into the close), rejecting late-stage and pump-and-dump setups. Use when the user asks what's trending, meme stocks, hype, social sentiment, or momentum plays. HIGH RISK by design. Research only, never places orders.
---

# momentum-hype

Code: `scanners/hype/` (`logic.py` confirmation / late-stage / attention score / pump flags / plan, `scan.py`),
`core/providers/attention.py` (ApeWisdom, Tradestie, StockTwits, Google News RSS, snapshot store), plus
`squeeze_context` and `call_activity` on the yfinance provider. Thresholds: `config/settings.yaml` under `hype:`.

## When to use
- "What's trending / what is Reddit talking about?" / "any meme stocks worth a look?" / "momentum plays?"
- As one of the four scanners in `run-all-scans`.
- Run it daily: each run saves an attention snapshot (`data/attention/`) and the true 30-day mention baseline only
  exists after several days of snapshots.

## Steps
1. Run the scan (a few minutes; news lookups are throttled):
   ```bash
   uv run python -m scanners.hype.scan
   ```
2. Read `reports/hype-YYYY-MM-DD.md` (funnel, ideas, hype without price confirmation, late-stage / pump rejects)
   and `-candidates.csv` (every name with its stage and reason).
3. Present every idea as **HIGH RISK**, with: what's driving attention (mentions and velocity vs baseline, news
   velocity, StockTwits rank, call volume vs open interest), the price confirmation (RVOL, day change, close in range),
   squeeze context (short % of float, days to cover; borrow fee unavailable), pump flags, and the plan: tight stop,
   half at 2R / rest at 3R, 5-session time stop. `risk-and-sizing` sizes these at 50% by default.
4. Be explicit about data limits:
   - until ~5 days of snapshots exist, velocity is vs **yesterday** (and Monday-vs-Sunday overstates it);
   - news counts come from Google News RSS (max 100 items, sometimes throttled);
   - tickers that are everyday words (AI, NOW, ALL…) can have inflated mention counts;
   - no Google Trends, no borrow fee, no direct Reddit API.
5. "Hype without price confirmation" is a watchlist, not a buy list: say so.

## Output
- `reports/hype-YYYY-MM-DD.json`: `Signal`s (skill = `momentum-hype`, setup `momentum_hype`) with `metrics`: mentions,
  mentions_baseline, baseline_mode, reddit_velocity, news_velocity, st_rank, wsb_comments, call_vol_oi, rvol,
  atrs_above_sma20, gain_5d, short_pct_float, days_to_cover, attention score parts. Two targets (scale-out).
- `reports/hype-YYYY-MM-DD.md`, `-candidates.csv`.

## Rules
- Never place or suggest placing orders. Never present hype alone as a reason to buy; price confirmation is required.
- Flag pump-and-dump signs plainly. Don't change thresholds without the user's approval.
