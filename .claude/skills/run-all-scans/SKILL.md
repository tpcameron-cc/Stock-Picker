---
name: run-all-scans
description: The daily pipeline — check the market regime (trend, breadth, VIX), run political-edge, breakout-scanner, momentum-hype and catalyst-plays in parallel, merge ideas by ticker with a confluence bonus and conflict flags, size them with risk-and-sizing (smaller in a weak regime), log every idea to the paper journal, and write one daily report. Use when the user says "run the scans", "what's on today", "daily report", or asks for the top ideas across all skills. Research only, never places orders.
---

# run-all-scans

Code: `scanners/run_all.py` (orchestrator), `core/regime.py` (regime), `core/risk.py`, `core/journal.py`.
Thresholds: `config/settings.yaml` under `regime:` and `orchestrator:`.

## When to use
- "Run the scans" / "what looks good today?" / "give me the daily report" / "top ideas".
- Best after the close (prices are final 20 minutes after 4 pm ET).

## Steps
1. Run it (the four scanners run as parallel processes; a full cold run takes ~15–20 min, cached reruns minutes):
   ```bash
   uv run python -m scanners.run_all                      # full
   uv run python -m scanners.run_all --quick              # cached data, first 1,500 listed stocks, top 5
   uv run python -m scanners.run_all --only breakout-scanner --only catalyst-plays
   uv run python -m scanners.run_all --skip-run --no-log  # re-merge today's outputs without journaling
   ```
   Run it in the background and wait for it to finish; don't start a second copy.
2. Read `reports/YYYY-MM-DD.md`:
   - **Regime first**: say whether it's strong / neutral / weak and what that changed (threshold raise, size cut).
   - Scanner status table: report any scanner that FAILED or produced no fresh output, with its log
     (`data/logs/<skill>.log`).
   - Top 10 ideas (5 in quick mode): thesis, plan, size, key dates, conflicts, flags, sources.
   - A section per skill and the rejected list with reasons. Ideas blocked by the political **sell blacklist**
     show as `blacklisted until <date>: <member> sold ...`; mention them.
3. Present the top ideas in order. For each, state which skills flagged it (confluence), any CONFLICT, the size
   (shares, $ and % risk), and that sizes are paper sizes. Call out momentum-hype ideas as HIGH RISK.
4. Mention how many ideas were logged to the journal and suggest `review-performance` weekly.

## Output
- `reports/YYYY-MM-DD.md` and `reports/YYYY-MM-DD.csv` (one row per ticker: skills, score, approved, shares,
  notional, risk $, plan, conflicts, sizing reasons, regime).
- Journal: every skill's idea is logged (taken = 1 only for the approved, sized primary idea).

## Rules
- Never place or suggest placing orders. Don't change thresholds without the user's approval.
- If a scanner failed, say so; never fill its section with guesses.
