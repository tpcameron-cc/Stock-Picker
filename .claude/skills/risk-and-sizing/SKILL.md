---
name: risk-and-sizing
description: Size stock ideas for paper trading and apply portfolio limits — fixed-% risk per idea from the entry-to-stop distance, 2:1 minimum reward:risk, max open positions, per-sector cap, and a correlation check against positions already in the paper journal. Use when the user asks how big a position would be, whether an idea fits the portfolio, to set their account size, or to log ideas to the paper journal. Also used by run-all-scans. Research only, never places orders.
---

# risk-and-sizing

Turns `Signal`s into paper position sizes and approves or rejects them against portfolio limits. Code:
`core/risk.py` (sizing, limits) and `core/journal.py` (paper-trade book). Thresholds: `config/settings.yaml`
under `risk:` and `journal:`.

## When to use
- "Size these ideas" / "how many shares of X?" / "does this fit my portfolio?"
- "Set my account size" (ask once; it is stored in the gitignored `config/local.yaml`).
- "Log today's ideas to the journal" / "how are my paper trades doing?"
- Called by `run-all-scans` on every surviving idea.

## Steps
1. **Account size.** If `account.size_usd` is unset, ask the user once, then store it:
   ```bash
   uv run python -m core.risk --set-account <USD>
   ```
   Never write it into `settings.yaml` (that file is committed). Without it, ideas still get approve/reject
   decisions but no share counts.
2. **Size ideas** from one or more Signal JSON files (this also refreshes the journal first, so closed trades
   free up slots):
   ```bash
   uv run python -m core.risk --signals reports/breakout-YYYY-MM-DD.json
   uv run python -m core.risk --signals reports/breakout-YYYY-MM-DD.json --log   # also record in the journal
   ```
   Use `--multiplier 0.5` when the market regime is weak (the orchestrator decides this).
3. Present: approved ideas with shares, notional, $ risk and % of account; rejected ideas with the reason;
   flags (unknown sector, unchecked correlation, size capped by the position limit).
4. **Journal** (paper trades only):
   ```bash
   uv run python -m core.journal update     # mark stop hit / target hit / time stop / expired from daily prices
   uv run python -m core.journal list       # active paper trades
   uv run python -m core.journal summary    # win rate, expectancy, vs SPY per skill and per signal
   ```
   With fewer than ~30 closed trades per skill, say plainly that it's too early to judge.

## Output
- `reports/sizing-YYYY-MM-DD.md`: one row per idea (approved, R:R, entry, stop, shares, notional, risk $, sector,
  max correlation and with which position, reasons / flags).
- Journal: SQLite `data/journal.sqlite`, table `paper_trades` (gitignored). Rejected ideas are logged with
  `taken = 0` so `review-performance` can test whether the filters help.

## Rules
- Sizing is arithmetic on the user's own rules, not a recommendation. Never place or suggest placing orders.
- Do not change risk limits without the user's approval.
