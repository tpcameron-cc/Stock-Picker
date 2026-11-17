"""Steps B and C: what each member oversees, and how each purchase relates to it.

DIRECT  the company received >= min_contract_usd of federal contract obligations from an agency under one of the
        member's committees (USAspending), or from any agency if the member sits on a whole-government committee.
SECTOR  the company's Yahoo sector / industry is under one of the member's committees (curated jurisdiction map).
NONE    neither.
Lobbying, campaign money, testimony, bills and hearing dates are NOT yet wired in; reports say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml


@dataclass
class Oversight:
    committees: list[str] = field(default_factory=list)       # names, subcommittees included
    leadership: list[str] = field(default_factory=list)       # "Chairman, House Committee on ..."
    agencies: set[str] = field(default_factory=set)
    sectors: set[str] = field(default_factory=set)
    industries: set[str] = field(default_factory=set)
    broad: bool = False
    codes: set[str] = field(default_factory=set)


def usd(v: float) -> str:
    """$1.2B / $163M / $0.4M — never '$0M' for a real amount."""
    if v >= 1e9:
        return f"${v / 1e9:,.1f}B"
    if v >= 1e7:
        return f"${v / 1e6:,.0f}M"
    return f"${v / 1e6:,.1f}M"


def load_jurisdiction(path: str | Path) -> dict[str, dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, dict] = {}
    for chamber in ("house", "senate"):
        for code, v in (data.get(chamber) or {}).items():
            out[code] = v or {}
    return out


def member_oversight(bioguide: str, assignments: pd.DataFrame, jurisdiction: dict[str, dict]) -> Oversight:
    ov = Oversight()
    rows = assignments[assignments["bioguide"] == bioguide]
    for r in rows.itertuples():
        ov.committees.append(r.name)
        if isinstance(r.title, str) and r.title:
            ov.leadership.append(f"{r.title}, {r.name}")
        for code in (r.code, r.parent_code):
            if isinstance(code, str) and code in jurisdiction:
                j = jurisdiction[code]
                ov.codes.add(code)
                ov.agencies |= set(j.get("agencies") or [])
                ov.sectors |= set(j.get("sectors") or [])
                ov.industries |= set(j.get("industries") or [])
                ov.broad = ov.broad or bool(j.get("broad"))
    return ov


def classify(ov: Oversight, sector: str | None, industry: str | None, contracts: pd.DataFrame | None,
             trade_date: date, p: dict) -> dict:
    """Classify one purchase. Returns {link, evidence, related_contracts, timing_flags, gaps}."""
    out = {"link": "none", "evidence": [], "timing_flags": [], "gaps": [], "flags": [], "related_contract_usd": 0.0,
           "committee_contract_share": None}
    related = pd.DataFrame()
    if contracts is None:
        out["gaps"].append("federal contract data unavailable")
    elif len(contracts):
        mask = contracts["agency"].isin(ov.agencies) | contracts["sub_agency"].isin(ov.agencies)
        related = contracts if ov.broad else contracts[mask]
        total = float(related["amount"].sum()) if len(related) else 0.0
        out["related_contract_usd"] = total
        all_total = float(contracts["amount"].sum())
        out["committee_contract_share"] = total / all_total if all_total > 0 else None
        if total >= float(p["min_contract_usd"]):
            by_ag = related.groupby("agency")["amount"].sum().sort_values(ascending=False)
            top = ", ".join(f"{a} {usd(v)}" for a, v in by_ag.head(3).items())
            scope = "any agency (whole-government committee)" if ov.broad and not mask.any() else "committee agencies"
            share = out["committee_contract_share"]
            out["link"] = "direct"
            out["evidence"].append(f"federal contracts from {scope}: {top} = {share:.0%} of its {usd(all_total)} "
                                   f"in federal contracts (USAspending, last {int(p['contracts_lookback_days']) // 365}y)")
            if share is not None and share < float(p["broad_vendor_share"]) and not ov.broad:
                out["flags"].append(f"broad government vendor: committee agencies are only {share:.0%} of its "
                                    f"federal contract dollars (weak direct link)")
    if out["link"] == "none":
        if sector and sector in ov.sectors:
            out["link"] = "sector"
            out["evidence"].append(f"sector '{sector}' under committee jurisdiction (curated map)")
        elif industry and industry in ov.industries:
            out["link"] = "sector"
            out["evidence"].append(f"industry '{industry}' under committee jurisdiction (curated map)")
        if not sector and not industry:
            out["gaps"].append("company sector/industry unavailable")

    # Timing: trade within N days BEFORE a sizeable related contract action
    if len(related):
        w = int(p["timing_window_days"])
        near = related[(related["action_date"] > trade_date) &
                       (related["action_date"] <= trade_date + timedelta(days=w)) &
                       (related["amount"] >= float(p["min_timing_action_usd"]))]
        for r in near.sort_values("amount", ascending=False).head(3).itertuples():
            out["timing_flags"].append(f"traded {(r.action_date - trade_date).days}d before {usd(r.amount)} "
                                       f"{r.agency} contract action ({r.action_date}) {r.url}")
    return out
