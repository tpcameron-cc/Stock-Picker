"""Attention data: Reddit/4chan mentions (ApeWisdom), r/wallstreetbets comments (Tradestie), StockTwits trending,
news headline counts (Google News RSS), and a daily snapshot store that builds the 30-day mention baseline.

All free, public, no keys. Each source can fail independently; callers treat a failure as a flagged gap.
"""

from __future__ import annotations

import collections
import json
import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import pandas as pd
import requests

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter

UA = {"User-Agent": "Mozilla/5.0 (stock-picker personal research script)"}
APEWISDOM = "https://apewisdom.io/api/v1.0/filter/{filter}/page/{page}"
TRADESTIE = "https://tradestie.com/api/v1/apps/reddit"
STOCKTWITS_TRENDING = "https://api.stocktwits.com/api/2/trending/symbols.json"
GOOGLE_NEWS = "https://news.google.com/rss/search"


def _get_json(url: str, limiter: RateLimiter, **kw):
    limiter.wait()
    r = requests.get(url, headers=UA, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


class AttentionProvider:
    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "attention")
        self.limiter = RateLimiter(1.0)
        self.news_limiter = RateLimiter(3.0)
        self.errors: list[str] = []

    def _safe(self, key: str, ttl: float, fetch, default):
        try:
            return self.cache.get_or_fetch(key, ttl, fetch).value
        except Exception as e:
            self.errors.append(f"{key.split('|')[0]}: {type(e).__name__}")
            return default

    # -- Reddit / 4chan mention leaderboard ---------------------------------------------------------------------
    def apewisdom(self, filter_: str = "all-stocks", pages: int = 3) -> pd.DataFrame:
        """ticker, name, rank, mentions (24h), mentions_24h_ago, upvotes, rank_24h_ago."""
        def fetch() -> pd.DataFrame:
            rows = []
            for p in range(1, pages + 1):
                j = _get_json(APEWISDOM.format(filter=filter_, page=p), self.limiter)
                rows += j.get("results", [])
                if p >= int(j.get("pages", 1)):
                    break
            df = pd.DataFrame(rows)
            if df.empty:
                return df
            for c in ("mentions", "mentions_24h_ago", "upvotes", "rank", "rank_24h_ago"):
                df[c] = pd.to_numeric(df.get(c), errors="coerce")
            df["ticker"] = df["ticker"].str.upper().str.replace(".", "-", regex=False)
            return df

        return self._safe(f"apewisdom|{filter_}|{pages}", 2, fetch, pd.DataFrame())

    # -- r/wallstreetbets comments + sentiment ------------------------------------------------------------------
    def wsb(self) -> pd.DataFrame:
        """ticker, no_of_comments, sentiment (Bullish/Bearish), sentiment_score."""
        def fetch() -> pd.DataFrame:
            df = pd.DataFrame(_get_json(TRADESTIE, self.limiter))
            if not df.empty:
                df["ticker"] = df["ticker"].str.upper()
            return df

        return self._safe("tradestie", 2, fetch, pd.DataFrame())

    # -- StockTwits trending ------------------------------------------------------------------------------------
    def stocktwits_trending(self) -> pd.DataFrame:
        """ticker, rank, trending_score, watchlist_count (top ~30)."""
        def fetch() -> pd.DataFrame:
            syms = _get_json(STOCKTWITS_TRENDING, self.limiter).get("symbols", [])
            return pd.DataFrame([{"ticker": s.get("symbol", "").upper(), "rank": s.get("rank"),
                                  "trending_score": s.get("trending_score"),
                                  "watchlist_count": s.get("watchlist_count")} for s in syms])

        return self._safe("stocktwits", 1, fetch, pd.DataFrame())

    # -- News headline counts -----------------------------------------------------------------------------------
    def news_counts(self, query: str, days: int = 30) -> dict | None:
        """Headline counts per day from Google News RSS for `query` over `days`. The feed returns at most 100 items,
        so for heavily covered names the window actually covered (`span_days`) can be shorter than requested."""
        def fetch() -> dict:
            self.news_limiter.wait()
            r = requests.get(GOOGLE_NEWS, params={"q": f"{query} when:{days}d", "hl": "en-US", "gl": "US",
                                                  "ceid": "US:en"}, headers=UA, timeout=30)
            r.raise_for_status()
            dates = [parsedate_to_datetime(d).date() for d in re.findall(r"<pubDate>(.*?)</pubDate>", r.text)]
            c = collections.Counter(dates)
            return {"counts": {d.isoformat(): n for d, n in c.items()}, "items": len(dates),
                    "capped": len(dates) >= 100}

        return self._safe(f"news|{query}|{days}", 6, fetch, None)


def news_velocity(news: dict | None, asof: date, days: int = 30) -> tuple[float | None, dict]:
    """Headlines on `asof` (and the day before, to catch after-hours news) per day vs the daily average of the
    rest of the covered window. Returns (velocity, detail)."""
    if not news or not news.get("counts"):
        return None, {"news_note": "news unavailable"}
    counts = {date.fromisoformat(k): v for k, v in news["counts"].items()}
    recent = counts.get(asof, 0) + counts.get(asof - timedelta(days=1), 0)
    older = {d: v for d, v in counts.items() if d < asof - timedelta(days=1)}
    if not older:
        return None, {"news_note": "no baseline headlines", "news_recent": recent}
    span = max((asof - timedelta(days=1) - min(older)).days, 1)
    base = sum(older.values()) / span
    detail = {"news_recent_2d": recent, "news_daily_baseline": round(base, 2), "news_span_days": span}
    if news.get("capped"):
        detail["news_note"] = f"feed capped at 100 items: baseline covers {span} days, not {days}"
    return (recent / 2) / base if base > 0 else None, detail


class SnapshotStore:
    """One JSON file per day (latest run wins): the day's attention leaderboard, kept to build mention baselines."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, day: date, payload: dict) -> Path:
        p = self.dir / f"{day.isoformat()}.json"
        p.write_text(json.dumps({"saved_at": datetime.now().isoformat(timespec="seconds"), **payload}), encoding="utf-8")
        return p

    def mentions_history(self, before: date, days: int) -> pd.DataFrame:
        """Rows: date, ticker, mentions — from saved ApeWisdom snapshots strictly before `before`."""
        rows = []
        for d in range(1, days + 1):
            day = before - timedelta(days=d)
            p = self.dir / f"{day.isoformat()}.json"
            if not p.exists():
                continue
            try:
                snap = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            for r in snap.get("apewisdom", []):
                rows.append({"date": day, "ticker": r.get("ticker"), "mentions": r.get("mentions")})
        return pd.DataFrame(rows, columns=["date", "ticker", "mentions"])
