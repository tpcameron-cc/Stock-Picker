---
name: political-edge
description: Find stocks recently bought by members of Congress whose disclosed trades beat the S&P 500, where the member's committees oversee the company (federal contracts from agencies under their committees = direct link; industry under committee jurisdiction = sector link). Built on House Periodic Transaction Reports parsed from the Clerk's PDFs. Use when the user asks about congressional trading, politician stock picks, committee conflicts, or "what is Congress buying". Research only, public data only, never places orders.
---

# political-edge

Code: `scanners/political/` (`screen.py` Step A, `oversight.py` Steps B–C, `score.py`, `scan.py`) and providers
`core/providers/house_disclosures.py` (PTR PDFs), `congress.py` (members, committees), `usaspending.py` (contracts).
Thresholds: `config/settings.yaml` under `political:`; committee map: `config/committee_jurisdiction.yaml` (curated).

## When to use
- "What are the best-performing members of Congress buying?" / "any committee-linked purchases?"
- "Is anyone on Armed Services buying defense names?" / "how has Rep. X's trading done?"
- As one of the four scanners in `run-all-scans`.

## Steps
1. Run the scan (first run downloads ~1,000 House PTR PDFs, ~15 min; PDFs are cached permanently):
   ```bash
   uv run python -m scanners.political.scan
   ```
2. Read `reports/political-YYYY-MM-DD.md` (ideas, Step A table, Step C purchases, gaps), `-members.csv`, `-purchases.csv`.
3. Present ideas with: member, party, seat, committees / leadership, the DIRECT or SECTOR evidence (contract
   amounts by agency, or the industry match), trade and filing dates, amount range, timing flags, other qualifying
   buyers, price move since disclosure, the plan, and the PTR PDF links.
4. Always state the method and its limits:
   - performance is **copy-trade** from disclosed trades (amount-range midpoints, trade-date prices; delay-adjusted
     uses filing dates), not the member's actual portfolio return; "portfolio" = PTR-traded book, not net worth;
   - House only (Senate needs the user's OK to accept the eFD terms); scanned paper PTRs are not read;
   - links use a curated committee map and name-matched USAspending recipients — label them as such;
   - lobbying, FEC money, testimony, bills/hearings, Trump-related overlays and "leaving office" are not covered yet.
5. Hand Signals to `risk-and-sizing`.

## Output
- `reports/political-YYYY-MM-DD.json`: `Signal`s (skill = `political-edge`) with `metrics`: link (direct|sector), member,
  bioguide, buyers, trade_date, filing_date, amount, filing_delay_days, move_since_disclosure, leadership, score parts.
- `-members.csv`: every member with trades — Step A metrics, qualified, fail reasons, flags, exception.
- `-purchases.csv`: every recent purchase by a qualifying member with its classification and evidence.

## Rules
- Public disclosures only. Never seek or infer nonpublic information; the committee link is a documented public
  overlap, not evidence of wrongdoing or inside knowledge — say so if the user asks.
- Personal, non-commercial research use only (5 U.S.C. 13107 restricts commercial use of these reports).
- Never place or suggest placing orders. Don't change thresholds or the committee map without the user's approval.
