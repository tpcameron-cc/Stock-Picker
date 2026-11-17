"""Federal contract obligations by recipient (USAspending.gov API, no key).

Company -> recipient matching is by NAME and therefore approximate: a recipient counts only if its
normalized name starts with the company's core name tokens (joint ventures and look-alikes are excluded).
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter

API = "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
AWARD_URL = "https://www.usaspending.gov/award/{gid}"
SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "plc", "ltd", "limited", "llc", "lp",
            "holdings", "holding", "group", "the", "sa", "nv", "ag", "se", "class", "common", "stock", "ordinary",
            "shares", "a", "b", "c", "new", "de"}


def core_name(company: str, max_tokens: int = 2) -> str:
    """'Lockheed Martin Corporation - Common Stock' -> 'lockheed martin'."""
    base = re.split(r"\s+-\s+", company or "")[0]
    toks = [t for t in re.findall(r"[a-z0-9&]+", base.lower()) if t not in SUFFIXES]
    return " ".join(toks[:max_tokens])


def recipient_matches(recipient: str, core: str) -> bool:
    if not core:
        return False
    toks = re.findall(r"[a-z0-9&]+", (recipient or "").lower())
    want = core.split()
    return toks[: len(want)] == want


class USAspendingProvider:
    name = "usaspending"
    source_url = "https://www.usaspending.gov/search"

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "usaspending")
        self.limiter = RateLimiter(0.5)

    def contracts(self, company: str, start: date, end: date, pages: int = 2) -> pd.DataFrame:
        """Contract transactions (largest first) whose recipient name matches the company's core name."""
        core = core_name(company)
        cols = ["award_id", "recipient", "action_date", "amount", "agency", "sub_agency", "url"]
        if not core:
            return pd.DataFrame(columns=cols)

        def fetch() -> pd.DataFrame:
            rows = []
            for page in range(1, pages + 1):
                self.limiter.wait()
                body = {"filters": {"recipient_search_text": [core], "award_type_codes": ["A", "B", "C", "D"],
                                    "time_period": [{"start_date": start.isoformat(), "end_date": end.isoformat()}]},
                        "fields": ["Award ID", "Recipient Name", "Action Date", "Transaction Amount",
                                   "Awarding Agency", "Awarding Sub Agency"],
                        "sort": "Transaction Amount", "order": "desc", "limit": 100, "page": page}
                r = requests.post(API, json=body, timeout=60)
                r.raise_for_status()
                j = r.json()
                for x in j.get("results", []):
                    rows.append({"award_id": x.get("Award ID"), "recipient": x.get("Recipient Name"),
                                 "action_date": pd.to_datetime(x.get("Action Date")).date() if x.get("Action Date") else None,
                                 "amount": float(x.get("Transaction Amount") or 0), "agency": x.get("Awarding Agency"),
                                 "sub_agency": x.get("Awarding Sub Agency"),
                                 "url": AWARD_URL.format(gid=x.get("generated_internal_id", ""))})
                if not (j.get("page_metadata") or {}).get("hasNext"):
                    break
            df = pd.DataFrame(rows, columns=cols)
            return df[df["recipient"].map(lambda r: recipient_matches(r, core))].reset_index(drop=True)

        return self.cache.get_or_fetch(f"contracts|{core}|{start}|{end}|{pages}", 24, fetch).value
