"""SEC EDGAR: ticker->CIK map and recent filings (used for dilution flags; Form 4 etc. later).

SEC fair-access policy requires a descriptive User-Agent with contact info, set via the env
var named in settings (providers.edgar.user_agent_env), and <= 10 requests/second.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


class EdgarUnavailable(RuntimeError):
    pass


class EdgarProvider:
    name = "sec_edgar"
    source_url = "https://www.sec.gov/edgar/search/"

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        p = self.cfg.section("providers.edgar")
        self.ttl = float(p["cache_ttl_hours"])
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "edgar")
        self.limiter = RateLimiter(float(p["min_seconds_between_calls"]))
        self.user_agent = self.cfg.env("providers.edgar.user_agent_env")

    def _get_json(self, url: str) -> dict:
        if not self.user_agent:
            raise EdgarUnavailable(
                f"Set env var {self.cfg.get('providers.edgar.user_agent_env')} "
                "(e.g. 'stock-picker you@example.com') to use SEC EDGAR."
            )
        self.limiter.wait()
        r = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=30)
        r.raise_for_status()
        return r.json()

    def cik_map(self) -> dict[str, int]:
        def fetch() -> dict[str, int]:
            data = self._get_json(TICKERS_URL)
            return {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}

        return self.cache.get_or_fetch("cik_map", self.ttl, fetch).value

    def recent_filings(self, ticker: str) -> pd.DataFrame:
        """Recent filings: columns form, filing_date (date), accession, url."""
        cik = self.cik_map().get(ticker.upper())
        if cik is None:
            return pd.DataFrame(columns=["form", "filing_date", "accession", "url"])

        def fetch() -> pd.DataFrame:
            recent = self._get_json(SUBMISSIONS_URL.format(cik=cik))["filings"]["recent"]
            df = pd.DataFrame({
                "form": recent["form"],
                "filing_date": pd.to_datetime(recent["filingDate"]).date,
                "accession": recent["accessionNumber"],
                "primary_doc": recent["primaryDocument"],
            })
            df["url"] = [
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{a.replace('-', '')}/{d}"
                for a, d in zip(df["accession"], df["primary_doc"])
            ]
            return df.drop(columns="primary_doc")

        return self.cache.get_or_fetch(f"filings|{cik}", self.ttl, fetch).value

    def dilutive_filings(self, ticker: str, forms: list[str], lookback_days: int,
                         today: date | None = None) -> pd.DataFrame:
        df = self.recent_filings(ticker)
        cutoff = (today or date.today()) - timedelta(days=lookback_days)
        return df[df["form"].isin(forms) & (df["filing_date"] >= cutoff)]
