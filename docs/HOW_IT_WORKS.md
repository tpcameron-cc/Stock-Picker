# How it works

A plain-language tour of each part of the project: what it looks at, why, and where it can go wrong. No code or finance background needed; unfamiliar terms are in the [glossary](GLOSSARY.md).

---

## Step 0: Narrowing ~6,000 stocks to the ones worth looking at

About 6,000 stocks trade on US exchanges. Many are tiny, rarely traded, or easy to manipulate. Before any scout looks, the project removes:

- stocks under **$3**,
- stocks where less than **$10 million** changes hands on an average day (hard to buy or sell without moving the price),
- anything not on a major exchange (no over-the-counter or "pink sheet" stocks),
- stocks without at least 60 days of price history.

It also **flags** warning signs: a recent reverse split (often a sign a struggling company is propping up its share price), or recent filings to sell new shares (dilution, which shrinks existing shareholders' slice). Flags lower confidence; they don't hide the stock, so you can see the warning.

## Step 1: The market weather check

Before picking individual stocks, the project asks seven yes/no questions about the market as a whole:

- Is the S&P 500 above its 50-day average, and is that average rising?
- Are most of the 11 major industry groups trending up (is the rally broad)?
- Is the average stock keeping up with the big names, or are a few giants carrying everything?
- Is the market's "fear gauge" (the VIX) calm, and is it falling?

5 or more "yes" = **strong**, 3–4 = **neutral**, 2 or fewer = **weak**. In a neutral market, ideas need a slightly higher score and positions are 75% of normal size. In a weak market, a much higher score and half size.

*Why:* most stocks follow the market's tide. Good ideas in a falling market often fail anyway.

---

## The four scouts

### 1. Breakout scout: "quiet strength near the highs"

**The idea.** Many big winners spent weeks moving sideways in a tight range near their highs (a *base*) before breaking out on heavy trading. Traders have studied this pattern for decades.

**What it requires first (leaders only):**
- The stock is in a healthy uptrend: its price is above its 50-day average, which is above its 150-day average, which is above its 200-day average.
- Over the last three months it beat at least **80%** of all other stocks.
- It's within reach of its 52-week high.

**Then it looks for one of:**
- **A base**: a tight, quiet range (flat base, a "cup with handle," or a series of ever-smaller pullbacks called a VCP),
- **A breakout**: today it pushed above the top of that range on at least 1.5× normal trading volume,
- **A pullback**: a strong stock dipping back to a rising average, a common "buy the dip in an uptrend" spot.

**The plan it writes:** buy just above the top of the range (not more than 5% above, or it's "too late"), stop below the range or two typical daily moves lower (never more than 10% below), and a target at least twice as far up as the stop is down.

**Honest result:** about break-even over 5 years of history (see the README). It's treated as a decent filter, not an edge on its own.

### 2. Catalyst scout: "earnings, with the odds checked"

**The idea.** Companies report results four times a year, and stocks often move sharply. Some companies have consistent habits: they beat expectations most quarters, their stock tends to drift up before the report, or it keeps rising for weeks after a big beat.

**What it checks for each company reporting in the next three weeks:**
- **Beat history**: how often did it beat analysts' estimates over the last ~12 quarters?
- **Estimate trend**: are analysts raising or cutting their forecasts going into the report?
- **Is the expected move cheap?** The options market prices in how big a move traders expect. The scout compares that with how much the stock *actually* moved on past reports.
- **Run-up habit**: did the stock tend to rise in the two weeks before past reports?
- **After-report drift**: after big beats, did it keep climbing?

**It also scans recent reporters** that beat by 5%+, jumped 3%+ on heavy volume, and *held* the gain (post-earnings drift).

**Negative checks:** an upcoming *lockup expiry* (when early investors are first allowed to sell after an IPO) or a recent share offering.

**Honest note:** earnings plays can't reach the usual 2-to-1 reward-to-risk rule (the target is a typical earnings move, about the same size as the risk), so they're allowed an exception at half size. Holding through a report is a coin flip with a thumb on the scale at best, and a gap can jump right past a stop.

### 3. Political scout: "public disclosures by members of Congress"

**The idea.** By law (the STOCK Act), members of Congress must publicly report their stock trades within 45 days. Some sit on committees that oversee the industries they invest in. These reports are public, so anyone can read them.

**What it does:**
1. Downloads every House and Senate trade report and reads the PDFs and web pages (thousands of them).
2. **Screens members** on how they trade: enough trades to judge, filing on time, a meaningful amount of money involved, and results that aren't driven by one lucky stock.
3. For each recent purchase, asks: **does this member's committee oversee this company?**
   - **Direct link:** the company has $10M+ in federal contracts from agencies under the member's committees.
   - **Sector link:** the company's industry falls under the committee's jurisdiction (for example, Armed Services and defense contractors).
4. Scores the purchase on that link, whether other members bought the same stock, the trade size, how recent it is, and whether company insiders are also buying. It takes points off for late filing or if the price already ran up.

**What we tested and changed:** it originally only followed members whose trades had beaten the S&P 500. A historical test (2023–2026, ~3,000 purchases) showed that past winners did no better on their *next* purchases, so that requirement was dropped and past performance now counts for zero points.

**The "do not touch" list:** if a qualifying member sells a large share (75%+) of a position worth more than $15,000, that stock is kept off every scout's list for 90 days.

**Important:** a committee link is a *documented public overlap*, not evidence of wrongdoing or inside knowledge. Reports can arrive weeks after the trade, so much of any information is already in the price.

### 4. Momentum / hype scout: "a crowd forming, confirmed by price"

**The idea.** Sometimes a stock suddenly becomes the talk of Reddit, StockTwits, or the news. Usually that's noise, but occasionally it's the start of a real move. The scout only cares when **the price agrees with the chatter.**

**Attention signals:** mentions on Reddit and 4chan vs their normal level, StockTwits trending rank, r/wallstreetbets comments, news-headline volume, and unusual buying of short-term call options.

**Price must confirm all of:** above its rising 10- and 20-day averages, at least **2×** normal trading volume today, and closing in the top 40% of the day's range (buyers held on into the close).

**It rejects:**
- **Late-stage moves**: already up 100%+ in a week, or stretched far above its average, or attention already fading from its peak.
- **Pump-and-dump signs**: tiny companies, penny-stock forum chatter. Two or more flags = rejected.

**The plan:** tight stop, quick targets, exit within ~5 trading days, at **half size**. This is high risk by design.

---

## Putting the ideas together

**Agreement and conflict.** If two or more scouts flag the same stock, it gets a bonus. If something contradicts an idea, it gets a penalty and a visible warning. Examples: a chart setup with earnings due in 3 days, hype while insiders are selling, or analysts cutting estimates.

**Fair ranking across scouts.** Each scout scores on its own 0–100 scale, and those scales don't match (catalyst scores run higher than breakout scores). So ideas are ranked by **priority**: how strong the idea is *compared with that scout's own past ideas* (a percentile), plus that scout's real track record once pretend trades close. A stock has to pass its own scout's bar to be considered at all.

**Sizing (the risk scout).** For each idea, in priority order:
- **How many shares?** Enough that hitting the stop loses about 0.75% of the account (less for riskier types or a weak market), and never more than 20% of the account in one stock.
- **Is the reward worth it?** The target must be at least 2× as far as the stop.
- **Does it fit?** At most 8 positions, at most 2 per industry, and no stock that moves almost identically to one already held.

Approved ideas become pretend positions. Rejected ideas are logged too, so the review can later check whether rejecting them was right.

---

## The journal and the weekly review

Every idea goes into a small database on your computer. Each day the project replays real prices against each plan, using the same rules a backtest uses:
- A pretend buy can only happen **after** the idea was written down (an evening idea can fill the next morning at the earliest).
- If the price hits the stop and the target on the same day, it assumes the worse one happened first.
- It charges a small cost on every pretend trade.

The **weekly review** reports, per scout: how often it wins, the average win and loss, the average result in *R* (multiples of the amount risked), how that compares with the S&P 500, and a confidence range. Until a scout has ~30 closed trades, the verdict is "too early." After that it's "adding value," "losing money," or "indistinguishable from noise." It also checks whether higher scores actually led to better results within each scout.

The review **suggests** changes; it never makes them. A person decides.

---

## Testing ideas against history

- **Backtest:** replays the breakout rules over 5 years of daily prices to see what would have happened, using only information available on each day.
- **Experiments:** tries variations (different stops, exits, entries) on one set of stocks, then checks them on a separate set so a lucky fit isn't mistaken for a real improvement.
- **Persistence test:** checks whether lawmakers who beat the market keep beating it.

Every result, including disappointing ones, is written into the project notes and drives decisions.
