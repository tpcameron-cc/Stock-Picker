---
name: catalyst-plays
description: Find earnings and event plays where the odds lean favorable — earnings in the next 3 weeks scored on beat history, estimate revisions, options-implied vs actual moves, pre-earnings run-ups and post-earnings drift; a post-earnings drift scan of recent big beats that gapped up and held; and negative flags for IPO lockup expiries and recent offerings. Use when the user asks about upcoming earnings, earnings plays, implied moves, post-earnings drift, or catalysts. Research only, never places orders.
---

# catalyst-plays

Code: `scanners/catalyst/` (`history.py` past reactions, `plays.py` scoring/plays/plans, `scan.py`).
Thresholds: `config/settings.yaml` under `catalyst:`. Data: Nasdaq earnings calendar, Yahoo earnings history,
estimates and option chains (via yfinance), SEC EDGAR for offerings (needs `SEC_USER_AGENT`).

## When to use
- "What's reporting soon that looks good?" / "earnings plays" / "is the implied move cheap on X?"
- "Any post-earnings drift setups?" / "who beat and held the gap?"
- As one of the four scanners in `run-all-scans`; breakout-scanner defers names reporting within 5 sessions here.

## Steps
1. Run the scan from the project root (first run is slow: ~7 Yahoo calls per name; cached 6–24h):
   ```bash
   uv run python -m scanners.catalyst.scan
   ```
2. Read `reports/catalyst-YYYY-MM-DD.md`. It has three parts:
   - **Actionable plays** (Signals): `run_up` (buy now, sell before the report), `hold_through` (hold through the
     report; always carries gap risk), `post_drift_entry` (already reported: big beat, gap up on volume, gap holding).
   - **Watchlist**: upcoming reporters where the odds aren't clear. The default play is to wait and trade
     post-earnings drift only if it beats and holds the gap. `runup_watch` = run-up pattern, window not open yet.
   - **Rejected** with reasons (estimates falling into the report = `avoid`).
3. Present each play with report date and timing, the play and why, beat rate, implied move vs average actual
   move, entry / stop / target / R:R, flags and sources. Say explicitly:
   - hold-through stops don't protect against an earnings gap;
   - "guidance" is a proxy (forward estimates rising after the report), not the company's guidance;
   - lockup dates are estimated (first trade + 180 days), not the filed date;
   - offering checks are off unless `SEC_USER_AGENT` is set.
4. Run-up and hold-through plays can't reach 2:1 by construction; `risk-and-sizing` exempts them and sizes
   them at 50% (drift entries still need 2:1). Hand actionable Signals to `risk-and-sizing` (`uv run python -m core.risk --signals reports/catalyst-YYYY-MM-DD.json`).

## Output
- `reports/catalyst-YYYY-MM-DD.json`: `Signal`s (skill = `catalyst-plays`) with `metrics`: play, setup, report_date,
  timing, quarters, beat_rate, avg_abs_move, implied_move, implied_ratio, avg_runup, runup_hit_rate,
  avg_drift_after_beat, revisions, score parts; drift entries add surprise_pct, gap, reaction_rvol, guidance_proxy.
- `reports/catalyst-YYYY-MM-DD.md`, `-watch.csv`, `-rejected.csv`.

## Not covered (say so if asked)
FDA PDUFA / advisory-committee dates, index additions, investor days and product launches (no free structured
source); federal contract awards are handled with `political-edge`.

## Rules
- Never place or suggest placing orders. Plans are research. Don't change thresholds without the user's approval.
