"""Exchange-listed symbol directory from Nasdaq Trader (public). OTC securities are never listed.

Files:
  nasdaqlisted.txt : Nasdaq-listed securities (exchange code assigned 'Q' here)
  otherlisted.txt  : NYSE (N), NYSE American (A), NYSE Arca (P), Cboe BZX (Z), IEX (V)
"""

from __future__ import annotations

import io

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache

BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"
SOURCE_URL = "https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs"


def _read_pipe(text: str) -> pd.DataFrame:
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("File Creation Time")]
    return pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str, keep_default_na=False)


def parse_directory(nasdaq_txt: str, other_txt: str) -> pd.DataFrame:
    """Normalize both files to columns: symbol, name, exchange, is_etf, is_test."""
    nq = _read_pipe(nasdaq_txt)
    nq = pd.DataFrame({
        "symbol": nq["Symbol"],
        "name": nq["Security Name"],
        "exchange": "Q",
        "is_etf": nq["ETF"].eq("Y"),
        "is_test": nq["Test Issue"].eq("Y"),
    })
    ot = _read_pipe(other_txt)
    ot = pd.DataFrame({
        "symbol": ot["ACT Symbol"],
        "name": ot["Security Name"],
        "exchange": ot["Exchange"],
        "is_etf": ot["ETF"].eq("Y"),
        "is_test": ot["Test Issue"].eq("Y"),
    })
    df = pd.concat([nq, ot], ignore_index=True)
    return df[df["symbol"].str.len() > 0].drop_duplicates("symbol").reset_index(drop=True)


class NasdaqTraderProvider:
    name = "nasdaqtrader"
    source_url = SOURCE_URL

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        self.ttl = float(self.cfg.get("providers.nasdaqtrader.cache_ttl_hours", 24))
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "nasdaqtrader")
        self.last_fetched_at = None

    def directory(self) -> pd.DataFrame:
        def fetch() -> pd.DataFrame:
            texts = []
            for f in ("nasdaqlisted.txt", "otherlisted.txt"):
                r = requests.get(BASE + f, timeout=30)
                r.raise_for_status()
                texts.append(r.text)
            return parse_directory(*texts)

        item = self.cache.get_or_fetch("directory", self.ttl, fetch)
        self.last_fetched_at = item.fetched_at
        return item.value
