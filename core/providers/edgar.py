"""SEC EDGAR: ticker->CIK map and recent filings (used for dilution flags; Form 4 etc. later).

SEC fair-access policy requires a descriptive User-Agent with contact info, set via the env
var named in settings (providers.edgar.user_agent_env), and <= 10 requests/second.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


class EdgarUnavailable(RuntimeError):
    pass


def parse_form4_purchases(xml_text: str) -> list[dict]:
    """Open-market purchases (non-derivative, transactionCode P) from a Form 4 XML document. Pure function."""
    return parse_form4_transactions(xml_text, "P")


def parse_form4_transactions(xml_text: str, code: str) -> list[dict]:
    """Non-derivative transactions with the given code (P = open-market purchase, S = open-market sale)."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    owner = (root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or "").strip()
    out = []
    for t in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        if (t.findtext("./transactionCoding/transactionCode") or "").strip() != code:
            continue
        try:
            shares = float(t.findtext("./transactionAmounts/transactionShares/value") or 0)
            price = float(t.findtext("./transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        out.append({"owner": owner, "trade_date": (t.findtext("./transactionDate/value") or "").strip(),
                    "shares": shares, "price": price, "value": shares * price})
    return out


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

    def insider_open_market_buys(self, ticker: str, lookback_days: int, today: date | None = None,
                                 max_filings: int = 20) -> pd.DataFrame:
        """Open-market purchases (Form 4, code P) filed in the lookback window."""
        return self.insider_transactions(ticker, "P", lookback_days, today, max_filings)

    def insider_transactions(self, ticker: str, code: str, lookback_days: int, today: date | None = None,
                             max_filings: int = 20) -> pd.DataFrame:
        """Form 4 non-derivative transactions with `code` (P buy / S sell) filed in the lookback window.
        Columns: filing_date, owner, trade_date, shares, price, value, url. Reads up to `max_filings` Form 4s."""
        cols = ["filing_date", "owner", "trade_date", "shares", "price", "value", "url"]
        df = self.recent_filings(ticker)
        cutoff = (today or date.today()) - timedelta(days=lookback_days)
        f4 = df[(df["form"] == "4") & (df["filing_date"] >= cutoff)].head(max_filings)
        rows = []
        for r in f4.itertuples():
            # primaryDocument is the XSL-rendered path (e.g. xslF345X05/x.xml); the raw XML sits one level up.
            raw_url = re.sub(r"/xslF345X\d+/", "/", r.url)

            def fetch(u=raw_url) -> str:
                if not self.user_agent:
                    raise EdgarUnavailable("SEC_USER_AGENT not set")
                self.limiter.wait()
                resp = requests.get(u, headers={"User-Agent": self.user_agent}, timeout=30)
                resp.raise_for_status()
                return resp.text

            xml = self.cache.get_or_fetch(f"form4|{raw_url}", 24 * 3650, fetch).value
            for t in parse_form4_transactions(xml, code):
                rows.append({"filing_date": r.filing_date, **t, "url": r.url})
        return pd.DataFrame(rows, columns=cols)

    def dilutive_filings(self, ticker: str, forms: list[str], lookback_days: int,
                         today: date | None = None) -> pd.DataFrame:
        df = self.recent_filings(ticker)
        cutoff = (today or date.today()) - timedelta(days=lookback_days)
        return df[df["form"].isin(forms) & (df["filing_date"] >= cutoff)]
