# Glossary

Plain-English definitions of the terms used in this project and its reports.

## Investing terms

**Stock / share / ticker.** A stock is a slice of ownership in a company. A ticker is its short code (e.g. MSFT for Microsoft).

**Paper trade.** A pretend trade, written down but never placed, to see how it would have done. Every trade in this project is a paper trade.

**Entry zone.** The price range where the plan says to buy. Above it, the idea is "extended" (too late).

**Stop (stop-loss).** The price where the plan admits it was wrong and exits, to keep a loss small.

**Target.** The price where the plan takes profit.

**Horizon / time stop.** The longest the plan will hold before exiting anyway.

**Risk (in dollars).** What you'd lose if the price fell from entry to the stop. Not the whole investment.

**R / R-multiple.** Results measured in units of the amount risked. Risk $100 and make $200: +2R. Hit the stop: −1R. This lets trades of different sizes be compared fairly.

**Reward-to-risk (R:R).** How far the target is compared with how far the stop is. 2:1 means the potential gain is twice the potential loss.

**Expectancy.** The average result per trade, in R or %. Positive = on average it makes money; around zero = break-even.

**Win rate / hit rate.** The share of trades that made money. A low win rate can still be profitable if the wins are much bigger than the losses.

**S&P 500 / SPY.** An index of 500 large US companies; SPY is a fund that tracks it. "Beating the market" usually means doing better than SPY over the same time.

**Excess return.** A return minus what SPY did over the same dates.

**Liquidity / dollar volume.** How much money changes hands in a stock each day. Liquid stocks are easier to buy and sell without moving the price.

**Penny stock / OTC.** Very cheap stocks, often traded "over the counter" outside major exchanges. Easier to manipulate; excluded here.

**Dilution.** A company issuing new shares, which shrinks each existing share's slice of the company. Often signalled by S-1, S-3 or 424B5 filings.

**Reverse split.** Combining shares (e.g. 10 old shares become 1) to raise the share price. Often a sign of a struggling company.

**Pump-and-dump.** Hyping a (usually tiny) stock to push its price up, then selling to the buyers who chased it.

**Earnings / earnings report.** A company's quarterly results. "Beating" means reporting more profit than analysts expected.

**Estimate revisions.** Analysts changing their forecasts. Rising estimates going into a report are generally a good sign.

**Implied move.** How big a move the options market expects around an event, worked out from option prices.

**Post-earnings drift.** The tendency of some stocks to keep moving in the same direction for weeks after a big earnings surprise.

**IPO lockup.** After a company first lists, insiders usually can't sell for about 180 days. When that ends, extra selling can pressure the price.

**Options / call options.** Contracts that pay off if a stock rises (calls) or falls (puts). Unusual call buying can signal bullish bets.

**Short interest / squeeze.** Shares borrowed and sold by people betting on a decline. If the price rises, they may be forced to buy back, pushing it higher (a squeeze).

**Insider buying (Form 4).** Company executives and directors must report their trades to the SEC on Form 4. Several insiders buying with their own money is often read as confidence.

**Sector / industry.** Groups of similar companies (Technology, Healthcare, Energy…). Holding many stocks from one sector means one piece of news can hit them all.

**Correlation.** How closely two stocks move together. Two highly correlated positions are nearly the same bet.

**VIX.** The "fear gauge": the market's expected volatility, from S&P 500 option prices. High or rising = nervous market.

**Market regime.** This project's label for the overall market (strong, neutral or weak), from trend, breadth and the VIX.

**Breadth.** How many stocks or sectors are joining a move. A rally led by a few giants is narrow; one where most sectors rise is broad.

## Chart-pattern terms

**Moving average (e.g. 50-day average).** The average closing price over the last N days, used to smooth out noise and show the trend. "Above a rising 50-day average" means a healthy uptrend.

**Relative strength (RS).** How a stock has done compared with the market or other stocks. "Top 20% RS" means it beat 80% of stocks.

**Base.** A period where a stock moves sideways in a fairly tight range, often near its highs, before its next move.

**Pivot.** The top of a base; a move above it is a breakout.

**Breakout.** The price pushing above a base's pivot, ideally on heavy volume.

**Pullback.** A temporary dip within an uptrend, often to a moving average.

**VCP (volatility contraction pattern).** A base where each pullback is smaller than the one before. The stock is "tightening up."

**Cup with handle.** A rounded dip and recovery (the cup) followed by a small, quiet dip (the handle), then a breakout.

**Volume / relative volume (RVOL).** How many shares traded. RVOL 2 = twice the normal amount.

**ATR (average true range).** A stock's typical daily price swing. Stops are often set a couple of ATRs away so normal wiggles don't trigger them.

**Gap.** When a stock opens well above or below the previous close, often after news. A gap can jump right past a stop.

## Congressional disclosure terms

**STOCK Act.** A 2012 law requiring members of Congress to publicly report stock trades over $1,000 within 45 days.

**PTR (Periodic Transaction Report).** The form members file to report those trades. Amounts are reported as ranges (e.g. $1,001–$15,000), not exact figures.

**eFD.** The Senate's electronic financial disclosure website.

**Committee jurisdiction.** The subjects and agencies a congressional committee oversees (e.g. Armed Services → the Department of Defense).

**Direct / sector link.** In this project: *direct* = the company gets $10M+ in federal contracts from agencies under the member's committees; *sector* = the company's industry falls under the committee's jurisdiction.

**Copy-trade performance.** How a member's disclosed trades would have done if copied, using the middle of each reported amount range. *Delay-adjusted* = copied at the filing date, when the public could actually see it.

## Testing and statistics terms

**Backtest.** Replaying a set of rules over historical prices to see what would have happened.

**Look-ahead bias.** Accidentally using information that wasn't available yet (tomorrow's price to decide today). This project has tests that guard against it.

**Survivorship bias.** Only testing companies that still exist today, which leaves out the failures and flatters results.

**Holdout / out-of-sample.** Data kept aside and only used to check a result found on other data. If an improvement only works on the data it was found on, it's probably luck.

**Walk-forward test.** Repeatedly deciding using only the past, then checking what happened next, step by step through time.

**Confidence interval (CI).** A range that likely contains the true value. If a 90% CI for "average result" includes zero, the data can't tell the effect from luck.

**Bootstrap.** A way to estimate a confidence interval by re-sampling the data many times.

**Shrinkage.** Pulling an estimate from a small sample toward a neutral value, because small samples swing wildly by chance. Five lucky trades shouldn't count as much as fifty.

**Percentile.** Where a value ranks among others. 80th percentile = higher than 80% of them.

**Calibration (score).** Making scores from different systems comparable. Here: ranking each idea by its percentile among its own scout's past scores.

**Rank correlation.** Whether higher values of one thing tend to go with higher values of another (+1 = always, 0 = no relationship, −1 = the opposite).

## Technical terms

**Terminal.** A text window for typing commands.

**Repository (repo).** A project folder tracked with Git.

**Python / uv.** Python is the programming language used here; uv installs Python and the project's libraries.

**Cache.** Saved copies of downloaded data, so it doesn't have to be downloaded again.

**Skill (Claude Code).** A written set of instructions the Claude Code assistant follows for a task like "run the scans."

**Signal.** The project's standard record for an idea: ticker, score, plan, reasons, warnings and sources.

**Journal.** The local database (`data/journal.sqlite`) of every idea and how it played out.
