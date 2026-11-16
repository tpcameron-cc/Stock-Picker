"""Shared plumbing for providers: on-disk cache with TTL, and a simple rate limiter."""

from __future__ import annotations

import hashlib
import pickle
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import pandas as pd


@dataclass
class Cached:
    value: Any
    fetched_at: datetime  # UTC; doubles as the as-of timestamp for citations


class DiskCache:
    """Pickle-per-key cache. Keys are hashed into file names under cache_dir/namespace."""

    def __init__(self, cache_dir: Path, namespace: str):
        self.dir = Path(cache_dir) / namespace
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha1(key.encode()).hexdigest() + ".pkl")

    def get(self, key: str, ttl_hours: float) -> Cached | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            with open(p, "rb") as fh:
                item: Cached = pickle.load(fh)
        except Exception:
            return None
        age_h = (datetime.now(timezone.utc) - item.fetched_at).total_seconds() / 3600
        return item if age_h < ttl_hours else None  # ttl 0 = always refetch

    def put(self, key: str, value: Any) -> Cached:
        item = Cached(value=value, fetched_at=datetime.now(timezone.utc))
        with open(self._path(key), "wb") as fh:
            pickle.dump(item, fh)
        return item

    def get_or_fetch(self, key: str, ttl_hours: float, fetch: Callable[[], Any]) -> Cached:
        hit = self.get(key, ttl_hours)
        return hit if hit is not None else self.put(key, fetch())


class RateLimiter:
    """Enforces a minimum interval between calls (thread-safe)."""

    def __init__(self, min_seconds: float):
        self.min_seconds = min_seconds
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delta = time.monotonic() - self._last
            if delta < self.min_seconds:
                time.sleep(self.min_seconds - delta)
            self._last = time.monotonic()


class PriceProvider(Protocol):
    """Interface any price source (yfinance, Polygon, Alpaca) must implement."""

    name: str
    source_url: str

    def history(self, tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
        """Daily OHLCV per ticker, lowercase columns, ascending DatetimeIndex. Missing tickers omitted."""
        ...

    def splits(self, ticker: str) -> pd.Series:
        """Split ratios indexed by date (e.g. 0.1 for a 1-for-10 reverse split)."""
        ...
