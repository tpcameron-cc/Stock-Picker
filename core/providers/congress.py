"""Current members of Congress and their committee assignments (unitedstates/congress-legislators, public domain).

    legislators-current.yaml          bioguide id, names, terms (chamber, state, district, party, end date)
    committees-current.yaml           committee / subcommittee codes and names
    committee-membership-current.yaml members of each committee with rank and title (Chair, Ranking Member...)
"""

from __future__ import annotations

import re
import unicodedata

import pandas as pd
import requests
import yaml

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache

BASE = "https://unitedstates.github.io/congress-legislators/"
SOURCE_URL = "https://github.com/unitedstates/congress-legislators"


def norm(name: str) -> str:
    """Lowercase ASCII letters only (accents stripped): 'Sánchez' -> 'sanchez'."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def members_frame(legislators: list[dict]) -> pd.DataFrame:
    rows = []
    for m in legislators:
        t = m["terms"][-1]
        n = m.get("name", {})
        rows.append({
            "bioguide": m["id"]["bioguide"], "first": n.get("first", ""), "last": n.get("last", ""),
            "nickname": n.get("nickname", ""), "full_name": n.get("official_full") or f"{n.get('first')} {n.get('last')}",
            "chamber": "house" if t["type"] == "rep" else "senate", "state": t["state"],
            "district": t.get("district"), "party": t.get("party"), "term_end": t.get("end"),
            "state_district": f"{t['state']}{int(t['district']):02d}" if t["type"] == "rep" and t.get("district") is not None
            else t["state"],
        })
    df = pd.DataFrame(rows)
    df["last_norm"] = df["last"].map(norm)
    return df


def committees_frame(committees: list[dict], membership: dict) -> pd.DataFrame:
    """One row per (member, committee or subcommittee): bioguide, code, name, parent_code, chamber, rank, title."""
    names, parent, chamber = {}, {}, {}
    for c in committees:
        code = c["thomas_id"]
        names[code], parent[code], chamber[code] = c["name"], None, c.get("type")
        for sc in c.get("subcommittees", []) or []:
            sub = code + sc["thomas_id"]
            names[sub], parent[sub], chamber[sub] = f"{c['name']} — {sc['name']}", code, c.get("type")
    rows = []
    for code, members in membership.items():
        for m in members or []:
            rows.append({"bioguide": m.get("bioguide"), "code": code, "name": names.get(code, code),
                         "parent_code": parent.get(code), "chamber": chamber.get(code), "rank": m.get("rank"),
                         "title": m.get("title"), "party_side": m.get("party")})
    return pd.DataFrame(rows)


def match_member(members: pd.DataFrame, last: str, state_district: str | None) -> str | None:
    """Bioguide id for a filer by last name + state/district (the House index's StateDst, e.g. 'CA11')."""
    cand = members[members["last_norm"] == norm(last)]
    if state_district:
        sd = members[members["state_district"] == state_district]
        both = cand[cand["state_district"] == state_district]
        if len(both) == 1:
            return both["bioguide"].iloc[0]
        # Compound / hyphenated surnames: unique member for the seat whose last name contains the filer's
        if len(sd) == 1 and (norm(last) in sd["last_norm"].iloc[0] or sd["last_norm"].iloc[0] in norm(last)):
            return sd["bioguide"].iloc[0]
    return cand["bioguide"].iloc[0] if len(cand) == 1 else None


class CongressProvider:
    name = "congress_legislators"
    source_url = SOURCE_URL

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "congress")
        self.last_fetched_at = None

    def _yaml(self, fname: str):
        def fetch():
            r = requests.get(BASE + fname, timeout=60, headers={"User-Agent": "stock-picker research script"})
            r.raise_for_status()
            return yaml.safe_load(r.content.decode("utf-8"))   # server omits charset; requests would guess latin-1

        item = self.cache.get_or_fetch(fname, 24, fetch)
        self.last_fetched_at = item.fetched_at
        return item.value

    def members(self) -> pd.DataFrame:
        return members_frame(self._yaml("legislators-current.yaml"))

    def committee_assignments(self) -> pd.DataFrame:
        return committees_frame(self._yaml("committees-current.yaml"), self._yaml("committee-membership-current.yaml"))
