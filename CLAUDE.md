# stock-picker

Research-only stock idea scanners. A set of Claude Code skills, each finding ideas from a
different angle, sharing one Python data layer (`core/`). Output is a ranked watchlist with
entry, stop, target and reasoning, plus a paper-trading journal.

## Role in this project

Act as a research analyst for the user. Run the scans, explain the reasoning behind any idea,
cite every number, and say plainly when data is missing or a skill is underperforming.

## Ground rules (non-negotiable)

- **No order execution.** Nothing here places trades or connects to a brokerage. Not even if asked.
- **Public information only.** Every signal comes from public, cited data. Never seek, infer or
  act on material nonpublic information.
- **Honest numbers.** Every figure carries a source URL and an as-of date. Missing or stale data is
  stated and lowers confidence; never fill gaps with estimates presented as facts. Do the math in
  code, not prose.
- **Filters on every scan:** liquidity floors and dilution / pump-and-dump flags (see `config/settings.yaml`).
- Not personalized investment advice.

## Layout

| Path | What |
|---|---|
| `config/settings.yaml` | Every threshold, the universe, risk limits. API keys are **env var names**, never values. |
| `core/config.py` | `settings()` / `load_settings()`; dotted access `cfg.get("filters.min_price")`, `cfg.env("providers.edgar.user_agent_env")`. |
| `core/signals.py` | `Signal` pydantic schema (the contract every skill outputs), `save_signals` → JSON + markdown. |
| `core/universe.py` | Builds the tradable universe: candidates → liquidity filters → risk flags. |
| `core/indicators.py` | Pure technical indicators (SMA/EMA, ATR, Bollinger, RSI, $vol, RVOL, MA stack, RS, VCP). |
| `core/simulate.py` | Shared trade simulator (entry fills, stops, targets, time stops, optional trailing exits). Used by the backtest AND the journal so both apply identical rules. |
| `core/risk.py` | risk-and-sizing: fixed-% risk sizing, position cap, 2:1 min R:R, max positions, sector cap, correlation check. CLI sizes Signal JSON files and can log them. |
| `core/journal.py` | Paper-trade journal (SQLite `data/journal.sqlite`, table `paper_trades`): log ideas, mark outcomes, stats per skill / setup / signal. |
| `config/committee_jurisdiction.yaml` | **Curated** committee → agencies / sectors / industries map (House Rule X, Senate Rule XXV). Editable; reports label links built from it. |
| `config/local.yaml` | **Gitignored** personal overrides (account size). Merged over `settings.yaml` by `settings()`. |
| `core/fundamentals.py` | Shared fundamental checks: YoY growth, deceleration, estimate revisions, earnings proximity. Missing data = `passed=None`, never a guess. |
| `scanners/<skill>/` | Python behind each skill (importable, tested). `scanners/breakout/`: `setups.py` (causal features, gates, plans, score), `scan.py`, `backtest.py`, `experiments.py` (exit/entry variants, read-only). `scanners/catalyst/`: `history.py` (past earnings reactions), `plays.py` (scores, plays, plans, drift, lockups), `scan.py`. `scanners/political/`: `screen.py` (Step A copy-trade screen), `oversight.py` (committee links), `score.py`, `scan.py`. |
| `core/providers/` | One module per data source, each with disk cache (`DiskCache`) + rate limiting (`RateLimiter`). `earnings_calendar.py` = Nasdaq earnings calendar; yfinance provider also has `earnings_history`, `implied_move`, `estimates`, `profile`. `house_disclosures.py` = House PTR index + PDF parser; `senate_disclosures.py` = Senate eFD PTRs (accepts the agreement); `congress.py` = members/committees; `usaspending.py` = federal contracts; `edgar.py` also parses Form 4 open-market buys. |
| `.claude/skills/` | One `SKILL.md` per skill; each calls into `scanners/` and `core/`. |
| `data/cache/` | Provider cache (gitignored). Delete a subfolder to force a refetch. |
| `data/journal.sqlite` | Paper-trade history (gitignored). Back it up; it is the only record of paper results. |
| `reports/` | Dated outputs (`universe-YYYY-MM-DD.csv`, later `YYYY-MM-DD.md/.csv`). |
| `tests/` | pytest suite. |

## Conventions

- Shared logic lives in `core/`; `SKILL.md` files stay short (what, when, steps, output) and call into `core/`.
- Any tunable number goes in `settings.yaml` with a comment, never hard-coded.
- Price frames: lowercase `open, high, low, close, volume`, ascending `DatetimeIndex`.
- Providers return data plus an as-of (`last_fetched_at`, last bar date). Today's bar is dropped if
  fetched before the close (`providers.yfinance.drop_incomplete_bar`).
- Data gaps become explicit flags (e.g. `"EDGAR check unavailable (...)"`), never silent passes.
  `reject_on_flag` only rejects on real findings, never on gaps.
- New price sources (Polygon, Alpaca) implement the `PriceProvider` protocol in `core/providers/base.py`.
- Scanner features must be **causal** (day t uses data <= t). The live scan and the backtest share the same
  feature/plan code; `tests/test_breakout.py::test_features_are_causal` guards against look-ahead.
- Skill-specific numbers go in `Signal.metrics` (e.g. pivot, distance_to_pivot, setup). For the journal every
  skill should also set `price_as_of`, `entry_mode` (`buy_stop` | `next_open`) and, if relevant, `cancel_below`.
- Personal values (account size) live in `config/local.yaml`, never in `settings.yaml`. Tests load
  `settings.yaml` explicitly (`load_settings(DEFAULT_SETTINGS)`) so they never see personal overrides.
- Journal outcomes are recomputed from prices until final; logging is idempotent per (signal_date, ticker, skill).

## Running

Python 3.12 via `uv` (on this PC uv was installed with winget; open a new terminal so it's on PATH).

```bash
uv sync                                   # install deps
uv run pytest                             # tests (offline; no network)
uv run python -m core.universe --source list            # quick: tickers in settings.universe.tickers
uv run python -m core.universe --limit 400              # first 400 exchange-listed stocks
uv run python -m core.universe                          # full universe (~5,900 candidates; ~15 min cold, cached 12h)
uv run python -m core.universe --source list --edgar    # + SEC dilutive-filing check (needs SEC_USER_AGENT)
uv run python -m scanners.breakout.scan --limit 600     # breakout scan (drop --limit for the full universe)
uv run python -m scanners.breakout.backtest --limit 1500  # 5y backtest on ~300 tickers (~3 min)
uv run python -m core.risk --set-account 5000           # stored in config/local.yaml (gitignored)
uv run python -m core.risk --signals reports/breakout-YYYY-MM-DD.json --log   # size + journal
uv run python -m core.journal update|list|summary       # paper-trade outcomes and stats
uv run python -m scanners.catalyst.scan                 # earnings plays + drift (~10 min cold, seconds when cached)
uv run python -m scanners.political.scan                # House PTRs (~15 min first run: ~1,000 PDFs, cached forever)
```

## Environment variables

| Var | Needed for | Required? |
|---|---|---|
| `SEC_USER_AGENT` | SEC EDGAR (e.g. `stock-picker you@example.com`) | Yes for dilution flags / Form 4 |
| `POLYGON_API_KEY` | Polygon prices (optional swap-in) | No |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca market data (optional) | No |
| `QUIVER_API_KEY` | Congressional trades fallback if InsiderCat MCP isn't available | No |

## Build status

- [x] Stage 1: core (config, Signal schema, universe + filters, indicators, providers: yfinance, Nasdaq Trader, EDGAR), tests
- [x] Stage 2: `breakout-scanner` + `backtest.py`
- [x] Stage 3: `risk-and-sizing` + `journal.py` (+ `core/simulate.py` shared by backtest and journal)
- [x] Stage 4: `catalyst-plays` (earnings plays, post-earnings drift, lockup / offering flags)
- [x] Stage 5: `political-edge` — House PTRs parsed from PDFs (InsiderCat has no active plan; user chose the free route)
- [ ] Stage 6: `momentum-hype`
- [ ] Stage 7: `run-all-scans` (+ `regime.py`) and `review-performance`

## Known limitations

- yfinance is unofficial and best-effort; Yahoo has no data for warrants/rights (excluded by name regex).
- Promotional-press-release detection (pump flag) is not implemented yet. Only reverse splits and
  dilutive SEC filings are flagged.
- `drop_incomplete_bar` doesn't know about early-close days (1 pm ET); those bars count as partial until 4:20 pm.
- The universe is today's listings, so any backtest built on it has survivorship bias.
- Breakout backtest (2026-11-16, 300 tickers, 5y, technical layer only): 514 trades, 36% hit rate,
  avg gain +10.9% / avg loss -7.0%, expectancy -0.10R (-0.56%/trade), -1.3% vs SPY over the same periods.
  Pullbacks ~breakeven (+0.01R); breakouts -0.25R; pre-breakouts -0.15R. Fundamentals were not applied.
  No thresholds have been changed in response; changes need the user's approval.
- Exit experiments (2026-11-16, `scanners/breakout/experiments.py`, report `reports/experiments-breakout-2026-11-16.md`):
  12 entry/exit variants on 300 tickers plus a 300-ticker holdout. No variant beat the baseline on both sets; patterns
  (by setup, by time half, by regime) flipped sign between sets. Baseline mean R 90% CI: main [-0.20, 0.00],
  holdout [-0.06, +0.16]. Treat the technical layer as roughly breakeven; exits unchanged.
- Yahoo gives ~5 quarters of revenue, so revenue deceleration is usually "unknown"; EPS uses ~16 quarters.
- catalyst-plays (2026-11-16): run-up and hold-through plays structurally fail the 2:1 R:R rule (targets are
  typical earnings moves, about the size of the risk; observed R:R 0.4–0.9). **User decision 2026-11-16:** exempt
  them (`risk.rr_exempt_setups`) — they qualify on historical odds via the catalyst play rules and are sized at
  50% (`risk.rr_exempt_size_multiplier`). Post-earnings-drift entries still need 2:1. Hold-through stops can be
  gapped through, so realized losses can exceed the planned (already halved) risk.
- catalyst-plays coverage gaps: no FDA PDUFA/AdComm dates, index additions, investor days or product launches
  (no free structured source); federal contracts belong to political-edge. "Guidance" is a proxy (forward
  estimates up after the report). Lockups are estimated as first trade + 180 days. Offering checks need SEC_USER_AGENT.
- Implied earnings move = ATM straddle with 60-day realized daily variance removed for the other sessions to
  expiry (`plays.event_implied_move`). Session counts use weekdays and ignore exchange holidays (e.g. Thanksgiving).
- political-edge data: InsiderCat MCP is connected but the account has no active plan (tools return no data);
  the user chose free parsing of House PTR PDFs (2026-11-16). Scanned paper PTRs have no text and are skipped.
  Senate eFD is covered (terms accepted by the user). Not yet covered: lobbying (LDA API was down), FEC money,
  testimony, visitor logs, bills / hearings (congress.gov key), Trump admin / mentions / sponsors (InsiderCat only),
  "leaving office". Personal, non-commercial use only (5 U.S.C. 13107).
- political-edge performance is copy-trade from disclosed trades (amount midpoints, trade-date prices; delay-adjusted
  = filing-date prices), vs SPY with identical cash flows. "Portfolio size" is the PTR-traded book, not net worth,
  so the $1M test is conservative and many members rely on the documented exceptions.

- political-edge first run (2026-11-16): 987 House PTRs for 2025–2026; all 863 e-filed parsed (11,452 transactions,
  9,653 with the account named), 124 scanned paper filings skipped. 69 members traded in the last year; 3 qualify
  (Franklin, Taylor, Moore; two via the small-portfolio exceptions). Same-day duplicate rows are real: the same
  stock bought in two accounts (e.g. a 401(k) and a joint brokerage). 61 traded tickers have no Yahoo prices
  (delisted / renamed / typos in filings) and are counted as unpriced, not guessed.
- "Direct" links from contracts include broad government vendors (e.g. AT&T: committee agencies = 12% of its
  federal contract $); these keep the spec's DIRECT label but carry a "broad government vendor" flag.
- risk-and-sizing tolerates cent-rounding in R:R exactly (`core.risk.rounding_slack`), so a 2R plan on a low-priced
  stock (rounded to 1.99) is not rejected while genuinely short targets still are.
- Senate eFD terms: on 2026-11-16 the user read the eFD agreement (Ethics in Government Act prohibitions: no
  unlawful or commercial use, no credit ratings, no soliciting money; civil penalty up to $10,000) and agreed, for
  personal non-commercial research, authorizing the scanner to accept the eFD agreement on their behalf.
  Senate coverage built the same day: 278 senator PTRs since 2025-01-01, all parsed (HTML tables), 2,736 transactions.
- Amended reports repeat the original's transactions in a new filing: identical transactions across different
  filings keep only the latest copy (`screen.dedupe_cross_filing`; 133 Senate + 14 House rows on 2026-11-16).
  Repeats inside one filing are kept (separate accounts). Former members (e.g. Sen. Mullin, Sen. Carper) are not in
  the current-members file and are excluded.
