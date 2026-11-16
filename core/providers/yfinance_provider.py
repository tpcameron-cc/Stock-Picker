"""Default price provider: Yahoo Finance via yfinance (free, unofficial, best-effort)."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import Settings, settings as default_settings
from core.providers.base import DiskCache, RateLimiter

log = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")


def drop_incomplete_bar(df: pd.DataFrame, fetched_at: datetime, buffer_minutes: int = 20) -> pd.DataFrame:
    """Drop the last daily bar if it was fetched before that session's 16:00 ET close (+buffer),
    i.e. it is a partial intraday bar. Early-close days are not special-cased (bar is kept)."""
    if df.empty:
        return df
    fetched_et = fetched_at.astimezone(NY)
    last_day = df.index[-1].date()
    cutoff = datetime.combine(last_day, time(16, 0), tzinfo=NY) + timedelta(minutes=buffer_minutes)
    return df.iloc[:-1] if fetched_et < cutoff else df


class YFinanceProvider:
    name = "yfinance"
    source_url = "https://finance.yahoo.com/"

    def __init__(self, cfg: Settings | None = None):
        self.cfg = cfg or default_settings()
        p = self.cfg.section("providers.yfinance")
        self.ttl = float(p["cache_ttl_hours"])
        self.cache = DiskCache(self.cfg.path_for("paths.cache_dir"), "yfinance")
        self.limiter = RateLimiter(float(p["min_seconds_between_calls"]))
        self.batch_size = int(self.cfg.get("universe.download_batch_size", 200))
        self.last_fetched_at = None  # as-of of the most recent history() result

    def history(self, tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
        out: dict[str, pd.DataFrame] = {}
        tickers = sorted({t.upper() for t in tickers})
        drop_partial = bool(self.cfg.get("providers.yfinance.drop_incomplete_bar", True))
        buffer_min = int(self.cfg.get("providers.yfinance.close_buffer_minutes", 20))
        for i in range(0, len(tickers), self.batch_size):
            batch = tickers[i : i + self.batch_size]
            key = f"history|{period}|{','.join(batch)}"
            item = self.cache.get_or_fetch(key, self.ttl, lambda b=batch: self._download(b, period))
            self.last_fetched_at = item.fetched_at
            for t, df in item.value.items():
                out[t] = drop_incomplete_bar(df, item.fetched_at, buffer_min) if drop_partial else df
        return out

    def _yf(self):
        import yfinance as yf

        # Keep yfinance's own timezone cache inside the project instead of %LOCALAPPDATA%.
        tz_dir = self.cfg.path_for("paths.cache_dir") / "yfinance_tz"
        tz_dir.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(tz_dir))
        return yf

    def _download(self, batch: list[str], period: str) -> dict[str, pd.DataFrame]:
        frames = self._download_once(batch, period, threads=True)
        missing = sorted(set(batch) - set(frames))
        if missing:
            # Threaded downloads sometimes fail transiently (e.g. sqlite "database is locked");
            # retry the misses once, single-threaded, before treating them as missing.
            frames.update(self._download_once(missing, period, threads=False))
        missing = set(batch) - set(frames)
        if missing:
            log.info("yfinance returned no data for %d tickers (e.g. %s)", len(missing), sorted(missing)[:5])
        return frames

    def _download_once(self, batch: list[str], period: str, threads: bool) -> dict[str, pd.DataFrame]:
        yf = self._yf()
        self.limiter.wait()
        raw = yf.download(
            batch, period=period, interval="1d", auto_adjust=True, group_by="ticker",
            threads=threads, progress=False,
        )
        frames: dict[str, pd.DataFrame] = {}
        for t in batch:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna(how="all")
            df = df.dropna(subset=["close"])
            if not df.empty:
                df.index = pd.to_datetime(df.index).tz_localize(None)
                frames[t] = df.sort_index()
        return frames

    def fundamentals(self, ticker: str) -> dict:
        """Raw fundamentals for core.fundamentals.evaluate_fundamentals. Each field may be None;
        `errors` lists what could not be fetched (data gaps are reported, never guessed)."""
        yf = self._yf()

        def fetch() -> dict:
            t = yf.Ticker(ticker)
            out: dict = {"errors": []}

            def grab(name, fn):
                try:
                    self.limiter.wait()
                    out[name] = fn()
                except Exception as e:
                    out[name] = None
                    out["errors"].append(f"{name}: {type(e).__name__}")

            def revenue():
                q = t.quarterly_income_stmt
                if q is None or q.empty or "Total Revenue" not in q.index:
                    return None
                s = q.loc["Total Revenue"].dropna().astype(float)
                s.index = pd.to_datetime(s.index)
                return s.sort_index()

            def eps_reported():
                ed = t.get_earnings_dates(limit=16)
                if ed is None or ed.empty:
                    return None
                s = ed["Reported EPS"].dropna().astype(float)
                s.index = pd.to_datetime(s.index, utc=True).tz_convert(None).normalize()
                return s.sort_index()

            def next_earnings():
                cal = t.calendar or {}
                dates = cal.get("Earnings Date") or []
                future = sorted(d for d in dates if d >= pd.Timestamp.now().date())
                return future[0] if future else None

            grab("revenue", revenue)
            grab("eps_reported", eps_reported)
            grab("eps_revisions", lambda: t.eps_revisions)
            grab("eps_trend", lambda: t.eps_trend)
            grab("next_earnings", next_earnings)
            return out

        item = self.cache.get_or_fetch(f"fundamentals|{ticker.upper()}", 24, fetch)
        return {**item.value, "fetched_at": item.fetched_at}

    def splits(self, ticker: str) -> pd.Series:
        yf = self._yf()

        def fetch() -> pd.Series:
            self.limiter.wait()
            s = yf.Ticker(ticker).splits
            if s is None or len(s) == 0:
                return pd.Series(dtype=float)
            s.index = pd.to_datetime(s.index).tz_localize(None)
            return s.astype(float)

        return self.cache.get_or_fetch(f"splits|{ticker.upper()}", self.ttl, fetch).value
