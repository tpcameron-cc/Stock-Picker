---
name: political-edge
description: Find stocks recently bought by members of Congress who pass a trading screen (past performance vs the S&P 500 is scored, or required in gate mode), where the member's committees oversee the company (federal contracts from agencies under their committees = direct link; industry under committee jurisdiction = sector link). Built on House Periodic Transaction Reports parsed from the Clerk's PDFs and Senate PTRs from the eFD site. Use when the user asks about congressional trading, politician stock picks, committee conflicts, "what is Congress buying", or whether members' outperformance persists. Research only, public data only, never places orders.
---

# political-edge

Code: `scanners/political/` (`screen.py` Step A, `oversight.py` Steps B–C, `score.py`, `scan.py`) and providers
`core/providers/house_disclosures.py` (PTR PDFs), `senate_disclosures.py` (eFD HTML reports), `congress.py` (members, committees), `usaspending.py` (contracts).
Thresholds: `config/settings.yaml` under `political:`; committee map: `config/committee_jurisdiction.yaml` (curated).

## When to use
- "What are the best-performing members of Congress buying?" / "any committee-linked purchases?"
- "Is anyone on Armed Services buying defense names?" / "how has Rep. X's trading done?"
- As one of the four scanners in `run-all-scans`.

## Steps
1. Run the scan (first run downloads ~1,000 House PTR PDFs and ~300 Senate reports, ~20 min; cached permanently):
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
   - House and Senate (Senate via eFD; the user accepted its terms 2026-11-16 for personal research); scanned /
     paper PTRs are not read; former members (not in the current-members file) are excluded;
   - links use a curated committee map and name-matched USAspending recipients — label them as such;
   - lobbying, FEC money, testimony, bills/hearings, Trump-related overlays and "leaving office" are not covered yet.
5. Report the **Sell blacklist** section: stocks a qualifying member sold big (range above $15k and >= 75% of
   their estimated position) are blocked from the daily list for 90 days. Say positions are estimates.
6. Political ideas hold up to 126 sessions and use a limit entry that waits up to 10 sessions for the zone.
7. Hand Signals to `risk-and-sizing`.
8. Say which `political.screen.performance_mode` is on. `score` (default since 2026-11-16): beating SPY is not
   required; the member's delay-adjusted excess vs SPY, shrunk for few buys, adds or subtracts up to
   `scoring.member_performance_max` points. `gate`: the member must beat SPY (the original screen). The report
   shows how many members the gate would have kept. The sell blacklist has its own member rule
   (`blacklist.members`, default `qualified_beat_spy`), so switching modes does not widen it.

## Does past performance persist? (walk-forward, read-only)
```bash
uv run python -m scanners.political.persistence            # political.persistence.years (default 4 filing years)
```
Quarterly formation dates; members screened on trades filed by each date; their next quarter's filed purchases
followed 126 sessions from the session after filing, vs SPY. Report `reports/political-persistence-YYYY-MM-DD.md`:
(A) beat vs trailed, (B) same on delay-adjusted excess, (C) kept vs dropped by the gate, rank correlation, and a
by-quarter table. CIs are member-cluster bootstraps. Report the verdicts and their CIs as they are; a CI spanning
zero means "not detectable with this data". Never change `performance_mode` or weights without the user's approval.

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
