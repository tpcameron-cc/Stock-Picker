# Prompt for Claude Code: build a "Stock Picker" project

Paste everything below the line into Claude Code, from inside an empty folder.

---

Build me a local project called **stock-picker**: a set of Claude Code skills that each find stock ideas from a different angle, sharing one data layer, plus one orchestrator skill that runs them all and combines the results. I'm technical (Python, SQL, R), so build it properly. Before writing code, show me a short plan and the list of API keys or accounts you'll need from me, then build it in stages.

## Ground rules (apply to everything)
- **Research and trade plans only. No order execution.** No skill places trades or connects to a brokerage. Output is a ranked watchlist with entry, stop, target and reasoning. A paper-trading journal tracks how the picks would have done.
- **Public information only.** Every signal must come from public, cited data. Never seek, infer or act on material nonpublic information.
- **Honest numbers.** Every figure carries its source and an as-of date. When data is missing or stale, say so and lower the confidence. Never fill gaps with estimates presented as facts. Do calculations in code, not prose.
- **Liquidity and safety filters on every scan.** By default: price ≥ $3, 20-day average dollar volume ≥ $10M, no OTC or pink sheets. Flag signs of dilution or a pump-and-dump: recent S-1, S-3 or ATM filings, reverse splits in the last 12 months, promotional press releases. All thresholds go in config.

## Project structure
```
stock-picker/
  CLAUDE.md                  # project overview, conventions, how to run each skill
  config/settings.yaml       # all thresholds, universe, risk limits, API keys via env vars
  core/                      # shared Python package
    providers/               # one module per data source, each with caching + rate limiting
    signals.py               # the standard Signal schema (below)
    universe.py              # builds the tradable universe after liquidity filters
    indicators.py            # shared technical indicators
    regime.py                # market regime check
    journal.py               # paper-trade log + performance tracking
  .claude/skills/
    political-edge/
    breakout-scanner/
    momentum-hype/
    catalyst-plays/
    risk-and-sizing/
    run-all-scans/
    review-performance/
  data/cache/                # gitignored
  reports/                   # dated markdown + CSV outputs
  tests/
```
Use Python 3.11+, `uv` for dependencies, `pandas`, `pydantic` for the schema, and `pytest`. Put every number that could be tuned in `settings.yaml`, not in code.

## Shared Signal schema
Every scanning skill outputs a list of these, saved as JSON plus a readable markdown summary:
`ticker, company, skill, score (0–100), confidence (low/med/high), thesis (2–3 sentences), signals_hit (list), entry_zone, stop, target(s), horizon (days), key_dates (earnings/catalyst/hearing), invalidation (what would kill the idea), risk_flags, sources (url + as-of date), generated_at`.

## Data providers (free first, paid optional)
Build an interface for each, and make the paid ones optional so the project still works without them:
- **Prices, fundamentals, options chains, earnings dates:** yfinance by default. Write the code so Polygon or Alpaca can be swapped in.
- **Congressional and executive trades:** use the InsiderCat MCP connector if it's available in the session (I have it set up). Otherwise use Quiver Quantitative's API if I add a key. As a last resort, parse House Clerk and Senate eFD periodic transaction reports directly.
- **Committee assignments:** congress.gov API and the unitedstates/congress-legislators YAML on GitHub.
- **Lobbying:** Senate LDA API. **Campaign money:** FEC API. **White House visitor logs** and published agency calendars where available.
- **Corporate insider trades and filings:** SEC EDGAR (Form 4, 8-K, S-1/S-3, 13D/G).
- **Federal contracts:** USAspending.gov API and the DoD daily contract announcements.
- **Hype and attention:** Reddit API (wallstreetbets, stocks, options, pennystocks as a warning signal), StockTwits trending, Google Trends (pytrends), news volume through a free news API, and options volume compared with open interest.
- **Catalyst calendars:** earnings dates; FDA PDUFA and advisory committee dates (biopharmcatalyst or the FDA calendar); index rebalance dates; IPO lockup expirations.

## Skill 1: `political-edge`
Find stocks recently bought by politicians whose committees or offices oversee those companies, or who have a documented public connection to them.

**Step A: screen politicians first, using my criteria:**
1. **Performance:** total return beats the S&P 500 over the same window. Use 1Y as the main window, and show 3Y if it's available.
2. **Reliability:** 1Y total return is close to 1Y delay-adjusted return (returns measured from the disclosure date). The default tolerance is ≤ 5 percentage points, configurable. If a source doesn't publish delay-adjusted returns, compute them from trade-date and disclosure-date prices.
3. **Size:** portfolio above $1M, using disclosure-range midpoints. Mark anyone whose range straddles $1M as borderline.
4. **Exceptions:** keep smaller portfolios that have strong performance or an unusual stock allocation. Debbie Wasserman Schultz and John Fetterman are the reference examples. Say why each exception was kept.
5. **Additional filters I want:**
   - at least 10 trades in the window
   - beats the S&P with their single best position removed (filters out people who simply own NVDA)
   - median filing delay of 30 days or less, with chronic late filers excluded
   - managed or blind-trust accounts and options-heavy books flagged
   - anyone leaving office in January flagged

**Step B: oversight and connection mapping.** For each politician who passes, list:
- their committees, subcommittees and leadership roles, and the agencies and industries each one has jurisdiction over
- bills they sponsored or co-sponsored

"Met with insiders" can't be observed directly, so use these public proxies instead and label them as proxies:
- the company lobbied the member's committee or on the member's bills (LDA)
- the company's PAC or executives donated to the member (FEC)
- the company's executives testified before the member's committee
- for executive-branch officials: White House visitor logs and published calendars

**Step C: classify each purchase from the last 90 days:**
- **Direct:** the company is regulated by, contracts with, or is the subject of legislation from a body the member sits on.
- **Sector:** the company is in an industry under the committee's jurisdiction, with no specific link to the company.
- **None.**

Flag timing: trades made within 30 days before a related hearing, markup, bill vote, contract award or regulatory decision.

**Step D: overlays:**
- the same ticker in Trump administration officials' portfolios
- Trump mentions of the company
- Trump sponsor or donor ties
- cluster buying: several qualifying politicians, or corporate insiders' Form 4 open-market buys in the same name

**Score:** overlap strength first, then agreement among buyers, size, recency and overlays. Penalize long filing delays and large price moves since disclosure.

## Skill 2: `breakout-scanner` (technical plus fundamentals)
Find stocks set up to break out, with fundamentals that support the move.

**Setups:**
- tight consolidation or volatility contraction near the 52-week high (Bollinger or ATR squeeze, volatility contraction pattern)
- flat-base and cup-with-handle breakouts
- 50 > 150 > 200-day moving average stack
- relative strength vs SPY in the top 20% of the universe, with the RS line at a new high
- breakout or pivot day on volume ≥ 1.5× the 50-day average
- pullbacks to a rising 21-day or 50-day average in leaders

**Fundamental filter:**
- revenue and EPS growth positive and not slowing sharply
- estimate revisions not falling
- no earnings report inside the next 5 trading days, unless flagged for Skill 4

**Output:** pivot price, stop based on the base low or ATR, measured-move target, and distance to pivot.

Make this skill **backtestable**: include a `backtest.py` that replays the scan historically and reports hit rate, average gain vs loss, and expectancy against SPY.

## Skill 3: `momentum-hype`
Find stocks where attention is accelerating and trade only the confirmed momentum, not the hype alone.

**Attention score:** mention velocity (current mentions vs a 30-day baseline) across Reddit, StockTwits, news and Google Trends, plus unusual call volume.

**Required confirmation from price action:**
- price above a rising 10-day and 20-day average
- relative volume ≥ 2×
- the stock holding its gains into the close

**Context:** short interest, days to cover, and borrow fee, to gauge squeeze potential.

**Late-stage filter (reject or flag):** price more than 3 ATRs above the 20-day average; up more than 100% in 5 days; mentions already past their peak; signs of a pump-and-dump.

**Trade plan:** smaller position size by default, tight stop, 3–10 day time stop, and scaling out into strength. Label every idea "high risk."

## Skill 4: `catalyst-plays`
Find earnings and event plays where the odds lean favorable.

**Earnings in the next 3 weeks.** Score each company on:
- its beat and raise history
- the trend in estimate revisions
- the options-implied move compared with the stock's average actual move in recent quarters (is the implied move cheap or rich?)
- its average post-earnings drift
- pre-earnings run-up patterns

Suggest one of three plays:
- buy the run-up and sell before the report
- hold through the report
- wait and trade post-earnings drift after the report

Post-earnings drift is the default when the odds aren't clear.

**Post-earnings drift scan:** companies that reported in the last 10 days with a big beat, raised guidance, a gap up on heavy volume, and price holding above the gap.

**Other catalysts:**
- FDA decision dates and advisory committee meetings
- investor days and product launches
- index additions
- large federal contract awards (link these to Skill 1 when a politician on the relevant committee holds the stock)
- lockup expirations and secondary offerings, as **negative** flags

## Skill 5: `risk-and-sizing`
Used by the orchestrator, and also runnable by itself.
- **Position size:** risk a fixed % of the account per trade (default 0.75%, configurable) using the distance to the stop.
- **Limits:** a maximum number of open positions, a cap per sector, and a correlation check against existing positions.
- **Risk/reward:** reject any idea below 2:1.
- **Account size:** ask me once, then store it in config.

## Skill 6: `run-all-scans` (orchestrator)
1. Check the market regime first. Examples: SPY vs its 50-day and 200-day averages, breadth, VIX level and trend. In a weak regime, raise score thresholds, cut sizes, and tell me so at the top of the report.
2. Run skills 1–4 in parallel as subagents, each returning Signal JSON.
3. Merge by ticker. **Confluence bonus** when two or more skills flag the same name. Mark any conflicts, such as a breakout with earnings in 3 days or hype with insider selling.
4. Run every surviving idea through `risk-and-sizing`.
5. Output `reports/YYYY-MM-DD.md` and `.csv` containing:
   - the market regime
   - the top 10 ideas, each with thesis, plan, size, key dates and sources
   - a section per skill
   - ideas that were rejected, with the reasons
6. Log every idea to the journal as a paper trade.

Support `--only <skill>` and `--quick` (cached data, top 5) modes.

## Skill 7: `review-performance`
Weekly feedback loop:
- Read the journal and mark each paper trade's outcome: stop hit, target hit, or time stop.
- Report win rate, expectancy, and return vs SPY **per skill and per signal type**.
- Tell me plainly which skills are adding value and which are noise.
- Suggest threshold changes. Don't apply them until I approve.

## Deliverables and checks
- `CLAUDE.md` explaining the project, how to run each skill, and where the config is.
- `settings.yaml` with comments on every threshold.
- Tests for indicators, the Signal schema, the filters and the journal math.
- A first dry run of `run-all-scans` using only free sources. Show me the report, and list which data sources worked, which failed, and which need keys.
- Keep each `SKILL.md` focused: what the skill does, when to use it, the steps, and the output format. Shared logic goes in `core/`, not copied into each skill.
