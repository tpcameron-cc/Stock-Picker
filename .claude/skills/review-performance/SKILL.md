---
name: review-performance
description: Weekly feedback loop on the paper-trading journal — mark each paper trade's outcome (stop, target, time stop, expired), report win rate, expectancy and return vs SPY per skill and per signal type, say plainly which skills add value and which are noise (bootstrap confidence intervals), and suggest threshold changes without applying them. Use when the user asks how the picks have done, which skills work, for a weekly review, or whether to change settings.
---

# review-performance

Code: `core/review.py` (verdicts, suggestions), `core/journal.py` (outcomes, stats), `core/simulate.py` (fill rules).
Thresholds: `config/settings.yaml` under `review:`.

## When to use
- "How are my picks doing?" / "which skills work?" / "weekly review" / "should I change anything?"
- Weekly, after `run-all-scans` has been logging ideas.

## Steps
1. Run it (refreshes outcomes from prices first):
   ```bash
   uv run python -m core.review
   ```
2. Read `reports/review-YYYY-MM-DD.md` and report:
   - each skill's verdict — **adding value**, **negative**, **inconclusive (noise)**, or **too early** — with closed
     trades, win rate, avg win / loss, expectancy in R and %, its 90% confidence interval, and return vs SPY;
   - per signal type and per setup;
   - whether risk-and-sizing helped (taken vs rejected-but-tracked ideas);
   - **does the score rank ideas within each skill?** (rank correlation of score vs R, top vs bottom half). The daily
     ranking uses each skill's score percentile, so a skill whose higher scores do WORSE is being mis-ranked: say so;
   - open and pending paper trades.
3. Be plain: fewer than 30 closed trades per skill means "too early"; a positive average whose interval spans zero
   is noise, not edge. Don't round a weak result up.
4. List the suggestions **as suggestions**. Apply a change to `config/settings.yaml` only after the user approves
   that specific change; then note it (date, old → new value, the evidence) in CLAUDE.md.

## Output
- `reports/review-YYYY-MM-DD.md`. The journal (`data/journal.sqlite`) is updated with outcomes; settings are never touched.
