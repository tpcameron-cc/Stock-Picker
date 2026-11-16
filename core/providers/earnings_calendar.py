"""Earnings calendar (Nasdaq public calendar API): who reports on which day, before or after the session.

One request per calendar day. Past days include reported EPS. No key needed; a generic User-Agent is sent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter

API = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
SOURCE_URL = "https://www.nasdaq.com/market-activity/earnings"
TIMING = {"time-pre-market": "bmo", "time-after-hours": "amc"}


def _money(x) -> float | None:
    if x in (None, "", "N/A"):
        return None
    try:
        return float(str(x).replace("$", "").replace(",", "").replace("(", "-").replace(")", ""))
    except ValueError:
        return None


def parse_day(rows: list[dict] | None, d: date) -> pd.DataFrame:
    cols = ["symbol", "date", "timing", "eps_forecast", "eps", "surprise_pct", "name"]
    if not rows:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame({
        "symbol": [str(r.get("symbol", "")).upper().strip() for r in rows],
        "date": d,
        "timing": [TIMING.get(r.get("time"), "unknown") for r in rows],
        "eps_forecast": [_money(r.get("epsForecast")) for r in rows],
        "eps": [_money(r.get("eps")) for r in rows],
        "surprise_pct": [_money(r.get("surprise")) for r in rows],
        "name": [r.get("name") for r in rows],
    })
    return out[out["symbol"] != ""][cols]


class EarningsCalendarProvider:
    name = "nasdaq_earnings_calendar"
    source_url = SOURCE_URL

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "earnings_calendar")
        self.limiter = RateLimiter(0.5)
        self.errors: list[str] = []

    def day(self, d: date) -> pd.DataFrame:
        ttl = 24 * 7 if d < date.today() - timedelta(days=3) else 12   # recent/future days can still change

        def fetch() -> pd.DataFrame:
            self.limiter.wait()
            r = requests.get(API.format(d=d.isoformat()), timeout=30,
                             headers={"User-Agent": "Mozilla/5.0 (stock-picker research script)",
                                      "Accept": "application/json"})
            r.raise_for_status()
            return parse_day(((r.json() or {}).get("data") or {}).get("rows"), d)

        try:
            return self.cache.get_or_fetch(f"day|{d.isoformat()}", ttl, fetch).value
        except Exception as e:
            self.errors.append(f"{d}: {type(e).__name__}")
            return parse_day(None, d)

    def range(self, start: date, end: date) -> pd.DataFrame:
        """All weekdays in [start, end]."""
        days = pd.bdate_range(start, end)
        frames = [self.day(x.date()) for x in days]
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else parse_day(None, start)
