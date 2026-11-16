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
| `core/providers/` | One module per data source, each with disk cache (`DiskCache`) + rate limiting (`RateLimiter`). |
| `.claude/skills/` | One folder per skill (built in later stages). |
| `data/cache/` | Provider cache (gitignored). Delete a subfolder to force a refetch. |
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

## Running

Python 3.12 via `uv` (on this PC uv was installed with winget; open a new terminal so it's on PATH).

```bash
uv sync                                   # install deps
uv run pytest                             # tests (offline; no network)
uv run python -m core.universe --source list            # quick: tickers in settings.universe.tickers
uv run python -m core.universe --limit 400              # first 400 exchange-listed stocks
uv run python -m core.universe                          # full universe (~5,900 candidates; ~15 min cold, cached 12h)
uv run python -m core.universe --source list --edgar    # + SEC dilutive-filing check (needs SEC_USER_AGENT)
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
- [ ] Stage 2: `breakout-scanner` + `backtest.py`
- [ ] Stage 3: `risk-and-sizing` + `journal.py`
- [ ] Stage 4: `catalyst-plays`
- [ ] Stage 5: `political-edge` (InsiderCat MCP is connected in Claude Code sessions)
- [ ] Stage 6: `momentum-hype`
- [ ] Stage 7: `run-all-scans` (+ `regime.py`) and `review-performance`

## Known limitations

- yfinance is unofficial and best-effort; Yahoo has no data for warrants/rights (excluded by name regex).
- Promotional-press-release detection (pump flag) is not implemented yet. Only reverse splits and
  dilutive SEC filings are flagged.
- `drop_incomplete_bar` doesn't know about early-close days (1 pm ET); those bars count as partial until 4:20 pm.
- The universe is today's listings, so any backtest built on it has survivorship bias.
