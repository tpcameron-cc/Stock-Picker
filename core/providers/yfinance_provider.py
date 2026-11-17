"""Default price provider: Yahoo Finance via yfinance (free, unofficial, best-effort)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
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


def straddle_from_chain(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, expiry) -> dict | None:
    """Implied move from the at-the-money straddle: (call mid + put mid) / spot at the strike nearest spot.
    Mid = (bid+ask)/2; if a side has no bid/ask, last price is used and the result is marked `used_last`."""
    if calls is None or puts is None or calls.empty or puts.empty or not spot or spot <= 0:
        return None
    common = sorted(set(calls["strike"]) & set(puts["strike"]))
    if not common:
        return None
    k = min(common, key=lambda s: abs(s - spot))
    c, p = calls[calls["strike"] == k].iloc[0], puts[puts["strike"] == k].iloc[0]
    used_last = False

    def mid(row) -> tuple[float | None, float | None]:
        nonlocal used_last
        b, a = float(row.get("bid") or 0), float(row.get("ask") or 0)
        if b > 0 and a > 0 and a >= b:
            return (a + b) / 2, (a - b)
        last = float(row.get("lastPrice") or 0)
        if last > 0:
            used_last = True
            return last, None
        return None, None

    cm, cs = mid(c)
    pm, ps = mid(p)
    if cm is None or pm is None:
        return None
    straddle = cm + pm
    spread_pct = ((cs or 0) + (ps or 0)) / straddle if cs is not None and ps is not None else None
    return {"move": straddle / spot, "expiry": expiry, "strike": float(k), "straddle_mid": straddle,
            "spot": float(spot), "spread_pct": spread_pct, "used_last": used_last}


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

    def profile(self, ticker: str) -> dict | None:
        """{sector, industry, first_trade_date} from Yahoo profile data; None on failure. Cached 7 days (failures aren't)."""
        key = f"profile2|{ticker.upper()}"
        hit = self.cache.get(key, 24 * 7)
        if hit is not None:
            return hit.value
        yf = self._yf()
        self.limiter.wait()
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            return None                      # transient failure: don't cache, try again next time
        ms = info.get("firstTradeDateMilliseconds")
        prof = {"sector": info.get("sector") or None, "industry": info.get("industry") or None,
                "first_trade_date": pd.Timestamp(ms, unit="ms").date() if ms else None}
        if prof["sector"] or prof["first_trade_date"]:
            self.cache.put(key, prof)
        return prof

    def sector(self, ticker: str) -> str | None:
        """GICS-style sector from Yahoo profile data; None if unavailable."""
        prof = self.profile(ticker)
        return prof["sector"] if prof else None

    def earnings_history(self, ticker: str, limit: int = 24) -> pd.DataFrame:
        """Past and next earnings events: date (session date, ET), timing (bmo|amc), eps_estimate,
        eps_reported, surprise_pct. Yahoo report times: before noon ET = bmo, otherwise amc. Cached 24h."""
        yf = self._yf()

        def fetch() -> pd.DataFrame:
            self.limiter.wait()
            ed = yf.Ticker(ticker).get_earnings_dates(limit=limit)
            if ed is None or ed.empty:
                return pd.DataFrame(columns=["date", "timing", "eps_estimate", "eps_reported", "surprise_pct"])
            idx = pd.to_datetime(ed.index, utc=True).tz_convert(NY)
            out = pd.DataFrame({
                "date": [ts.date() for ts in idx],
                "timing": ["bmo" if ts.hour < 12 else "amc" for ts in idx],
                "eps_estimate": ed["EPS Estimate"].astype(float).to_numpy(),
                "eps_reported": ed["Reported EPS"].astype(float).to_numpy(),
                "surprise_pct": ed["Surprise(%)"].astype(float).to_numpy(),
            })
            return out.sort_values("date").reset_index(drop=True)

        return self.cache.get_or_fetch(f"earnings_history|{ticker.upper()}|{limit}", 24, fetch).value

    def implied_move(self, ticker: str, on_or_after: date) -> dict | None:
        """ATM straddle mid / spot for the first expiry on or after `on_or_after` (the earnings reaction day).
        Returns {move, expiry, strike, straddle_mid, spot, spread_pct} or None if no usable chain. Cached 6h."""
        yf = self._yf()

        def fetch() -> dict | None:
            t = yf.Ticker(ticker)
            self.limiter.wait()
            expiries = [date.fromisoformat(e) for e in (t.options or ())]
            expiries = [e for e in expiries if e >= on_or_after]
            if not expiries:
                return None
            exp = expiries[0]
            self.limiter.wait()
            spot = float(t.fast_info["last_price"])
            ch = t.option_chain(exp.isoformat())
            return straddle_from_chain(ch.calls, ch.puts, spot, exp)

        try:
            return self.cache.get_or_fetch(f"implied|{ticker.upper()}|{on_or_after}", 6, fetch).value
        except Exception as e:
            log.info("implied move unavailable for %s: %s", ticker, type(e).__name__)
            return None

    def squeeze_context(self, ticker: str) -> dict | None:
        """Short interest (% of float, days to cover, as-of date) and market cap from Yahoo. Cached 24h.
        Borrow fee is NOT available from free sources."""
        yf = self._yf()

        def fetch() -> dict:
            self.limiter.wait()
            i = yf.Ticker(ticker).info or {}
            ts = i.get("dateShortInterest")
            return {"market_cap": i.get("marketCap"), "short_pct_float": i.get("shortPercentOfFloat"),
                    "days_to_cover": i.get("shortRatio"), "shares_short": i.get("sharesShort"),
                    "short_interest_date": pd.Timestamp(ts, unit="s").date().isoformat() if ts else None,
                    "float_shares": i.get("floatShares")}

        try:
            return self.cache.get_or_fetch(f"squeeze|{ticker.upper()}", 24, fetch).value
        except Exception as e:
            log.info("squeeze context unavailable for %s: %s", ticker, type(e).__name__)
            return None

    def call_activity(self, ticker: str, expiries: int = 2) -> dict | None:
        """Today's option activity over the nearest `expiries` expirations: call volume, call open interest,
        put volume. Cached 6h."""
        yf = self._yf()

        def fetch() -> dict | None:
            t = yf.Ticker(ticker)
            self.limiter.wait()
            exps = list(t.options or ())[:expiries]
            if not exps:
                return None
            cv = coi = pv = 0.0
            for e in exps:
                self.limiter.wait()
                ch = t.option_chain(e)
                cv += float(ch.calls["volume"].fillna(0).sum())
                coi += float(ch.calls["openInterest"].fillna(0).sum())
                pv += float(ch.puts["volume"].fillna(0).sum())
            return {"call_volume": cv, "call_oi": coi, "put_volume": pv, "expiries": exps}

        try:
            return self.cache.get_or_fetch(f"calls|{ticker.upper()}|{expiries}", 6, fetch).value
        except Exception as e:
            log.info("option activity unavailable for %s: %s", ticker, type(e).__name__)
            return None

    def estimates(self, ticker: str) -> dict:
        """Just the consensus tables: {eps_trend, eps_revisions, errors}. Two calls instead of fundamentals()'
        five; reuses a fresh fundamentals() cache entry if one exists. Cached 24h."""
        hit = self.cache.get(f"fundamentals|{ticker.upper()}", 24)
        if hit is not None:
            return {k: hit.value.get(k) for k in ("eps_trend", "eps_revisions", "errors")}
        yf = self._yf()

        def fetch() -> dict:
            t = yf.Ticker(ticker)
            out: dict = {"errors": []}
            for name in ("eps_trend", "eps_revisions"):
                try:
                    self.limiter.wait()
                    out[name] = getattr(t, name)
                except Exception as e:
                    out[name] = None
                    out["errors"].append(f"{name}: {type(e).__name__}")
            return out

        return self.cache.get_or_fetch(f"estimates|{ticker.upper()}", 24, fetch).value

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
