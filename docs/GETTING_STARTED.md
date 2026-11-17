# Getting started

A step-by-step guide for someone who has never used a terminal. Plan on about 30 minutes for setup, plus a longer first run while the project downloads its data.

> Everything here is pretend money. Nothing can place a real trade.

---

## What you'll install

| Tool | What it is | Why |
|---|---|---|
| **Git** | A tool for downloading and tracking code | To get a copy of this project |
| **uv** | A tool that installs Python and this project's add-ons for you | So you don't have to set up Python by hand |
| **Claude Code** *(optional)* | An AI assistant that runs in a terminal or desktop app | To run everything by asking in plain English |

---

## 1. Open a terminal

A terminal is a window where you type commands instead of clicking.

- **Windows:** press the Windows key, type **Terminal** (or **PowerShell**), and press Enter.
- **Mac:** press Cmd + Space, type **Terminal**, and press Enter.

To run a command, type (or paste) it and press Enter.

## 2. Install Git and uv

**Windows** (paste one line at a time):

```bash
winget install Git.Git
```

```bash
winget install astral-sh.uv
```

**Mac:**

```bash
xcode-select --install
```

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then **close the terminal and open a new one**, so it notices the new tools. Check they're there:

```bash
git --version
```

```bash
uv --version
```

Each should print a version number. If you see "not recognized" or "command not found," close and reopen the terminal once more.

## 3. Download the project

This copies the project into a folder called `Stock-Picker` and moves you into it. (If the repository is private, the owner has to give your GitHub account access first, and Git will ask you to sign in.)

```bash
git clone https://github.com/tpcameron-cc/Stock-Picker.git
```

```bash
cd Stock-Picker
```

## 4. Install the project's add-ons

```bash
uv sync
```

This downloads the right version of Python and the libraries the project uses. It takes a minute or two the first time.

## 5. Make sure everything works

```bash
uv run pytest
```

This runs the project's built-in self-checks (no internet needed). You should see a row of dots and no `FAILED` lines.

## 6. Tell it your pretend account size

Sizes are worked out as a share of an account. Pick any number; it's pretend. For example, $10,000:

```bash
uv run python -m core.risk --set-account 10000
```

This is saved in `config/local.yaml`, a personal file that never gets uploaded anywhere.

## 7. (Recommended) Let it read SEC filings

The SEC asks automated tools to identify themselves with a name and email. Without this, dilution warnings and insider-buying checks are skipped (the report will say so).

**Windows** (replace with your own name and email):

```bash
setx SEC_USER_AGENT "stock-picker your-name@example.com"
```

**Mac:**

```bash
echo 'export SEC_USER_AGENT="stock-picker your-name@example.com"' >> ~/.zshrc
```

Then close and reopen the terminal.

---

## 8. Run it

### A quick test run (a few minutes)

Uses a smaller set of stocks and whatever data is already saved:

```bash
uv run python -m scanners.run_all --quick
```

### The full daily run (after the market closes)

Run it after 4:20 pm US Eastern time on a weekday, so the day's prices are final:

```bash
uv run python -m scanners.run_all
```

**The first full run is slow (roughly 20–40 minutes).** It downloads price history for thousands of stocks and about 1,000+ congressional disclosure files. Everything is saved, so later runs take a few minutes.

### Other things you can run

| To… | Run |
|---|---|
| See today's market "weather" only | `uv run python -m core.regime` |
| Just the breakout scout | `uv run python -m scanners.breakout.scan` |
| Just the earnings scout | `uv run python -m scanners.catalyst.scan` |
| Just the political scout | `uv run python -m scanners.political.scan` |
| Just the hype scout (best run daily: it learns what "normal" chatter is) | `uv run python -m scanners.hype.scan` |
| Update and list your pretend trades | `uv run python -m core.journal update` then `uv run python -m core.journal list` |
| Weekly scorecard | `uv run python -m core.review` |
| Test the breakout rules on 5 years of history | `uv run python -m scanners.breakout.backtest --limit 1500` |
| Test whether lawmakers' past results predict anything | `uv run python -m scanners.political.persistence` |

---

## 9. Read the results

Everything is written to the **`reports`** folder, named by date:

- **`reports/YYYY-MM-DD.md`**: the daily report. Market weather first, then the top ideas with plans, sizes, warnings, and sources. Open it in any text editor, or in VS Code / GitHub for nicer formatting.
- **`reports/YYYY-MM-DD.csv`**: the same ideas as a spreadsheet (opens in Excel or Google Sheets).
- Each scout also writes its own detailed report (e.g. `reports/political-YYYY-MM-DD.md`).
- **`reports/review-YYYY-MM-DD.md`**: the weekly scorecard.

Your pretend-trade history lives in `data/journal.sqlite`. **Back this file up**; it's the only record of your paper results.

---

## The easy way: ask Claude Code

If you have [Claude Code](https://claude.com/claude-code), open this folder in it and just ask:

- "Run the scans."
- "What's Congress buying, and which committees are involved?"
- "Any earnings plays in the next three weeks?"
- "What's trending, and is the price confirming it?"
- "How would a position in [ticker] be sized?"
- "How have the picks done this week?"
- "Explain why you ranked this idea first."

The assistant follows the instructions in `.claude/skills/`, runs the right commands, and explains the results, citing a source for every number.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `uv` or `git` "not recognized" | Close and reopen the terminal. Still failing? Restart the computer. |
| A scout says "no fresh output" or "FAILED" | Look at its log in `data/logs/<scout-name>.log`. Often a data website was temporarily down; run it again later. |
| Data looks a day old | Yahoo sometimes publishes the day's prices late in the evening. The report flags stocks whose latest price lags. Re-run later. |
| "EDGAR check unavailable" in reports | Set `SEC_USER_AGENT` (step 7). |
| You want to force fresh data | Delete the matching folder inside `data/cache/`. It will re-download. |
| Every scout found nothing | That happens on weak or choppy days. "No good ideas today" is a valid answer. |

---

## Changing the rules

Every number the project uses (minimum price, risk per trade, how many positions, score cutoffs) lives in **`config/settings.yaml`**, with a comment explaining each one. Open it in a text editor to read or change them.

A word of caution: the project's history shows that most "improvements" that look good on past data don't hold up. Test a change (backtest, or a few weeks of paper trading) before trusting it.
