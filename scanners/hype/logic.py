"""momentum-hype logic: price confirmation, late-stage filter, attention score, pump flags, plan. Pure functions."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from core import indicators as ind


@dataclass
class HypePlan:
    entry_low: float
    entry_high: float
    stop: float
    targets: list[float]
    horizon_days: int
    stop_basis: str
    flags: list[str] = field(default_factory=list)


def confirmation(df: pd.DataFrame, c: dict) -> tuple[list[str], dict]:
    """Price must confirm the attention. Returns (failures, detail). Empty failures = confirmed."""
    k = c["confirm"]
    close, high, low = df["close"], df["high"], df["low"]
    fast, slow = ind.sma(close, int(k["sma_fast"])), ind.sma(close, int(k["sma_slow"]))
    lb = int(k["rising_lookback"])
    rvol = ind.relative_volume(df, 50)
    last = len(df) - 1
    rng = high.iloc[-1] - low.iloc[-1]
    pos = (close.iloc[-1] - low.iloc[-1]) / rng if rng > 0 else np.nan
    d = {"close": float(close.iloc[-1]), "sma10": float(fast.iloc[-1]), "sma20": float(slow.iloc[-1]),
         "rvol": float(rvol.iloc[-1]) if pd.notna(rvol.iloc[-1]) else np.nan, "close_in_range": float(pos),
         "day_change": float(close.iloc[-1] / close.iloc[-2] - 1) if last >= 1 else np.nan}
    fails = []
    if not (close.iloc[-1] > fast.iloc[-1] and close.iloc[-1] > slow.iloc[-1]):
        fails.append("close not above the 10- and 20-day averages")
    if not (fast.iloc[-1] > fast.iloc[-1 - lb] and slow.iloc[-1] > slow.iloc[-1 - lb]):
        fails.append("10/20-day averages not rising")
    if not (d["rvol"] >= float(k["min_rvol"])):
        fails.append(f"relative volume {d['rvol']:.1f}x < {k['min_rvol']}x")
    if not (pos >= float(k["close_in_range_min"])) or not (d["day_change"] > 0):
        fails.append(f"didn't hold gains into the close (closed at {pos:.0%} of the range, {d['day_change']:+.1%} on the day)")
    return fails, d


def late_stage(df: pd.DataFrame, mentions: float | None, mentions_peak: float | None, mentions_yday: float | None,
               c: dict, atr_period: int = 14) -> tuple[list[str], dict]:
    """Too late to chase? Returns (reasons, detail). Any reason = reject."""
    L = c["late_stage"]
    close = df["close"]
    atr = float(ind.atr(df, atr_period).iloc[-1])
    sma20 = float(ind.sma(close, 20).iloc[-1])
    ext = (float(close.iloc[-1]) - sma20) / atr if atr > 0 else np.nan
    gain5 = float(close.iloc[-1] / close.iloc[-6] - 1) if len(close) > 5 else np.nan
    d = {"atr": atr, "atrs_above_sma20": ext, "gain_5d": gain5}
    out = []
    if ext > float(L["max_atr_above_sma20"]):
        out.append(f"extended: {ext:.1f} ATRs above the 20-day average (> {L['max_atr_above_sma20']})")
    if gain5 > float(L["max_gain_5d"]):
        out.append(f"up {gain5:.0%} in 5 sessions (> {float(L['max_gain_5d']):.0%})")
    fr = float(L["fade_ratio"])
    if mentions is not None and mentions_peak and mentions < fr * mentions_peak:
        out.append(f"attention past its peak: {mentions:.0f} mentions vs recent peak {mentions_peak:.0f}")
    elif mentions is not None and mentions_yday and mentions < fr * mentions_yday:
        out.append(f"attention fading: {mentions:.0f} mentions vs {mentions_yday:.0f} yesterday")
    return out, d


def _scaled(x, lo, hi, pts) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return 0.0
    return float(np.clip((x - lo) / (hi - lo), 0, 1) * pts)


def attention_score(reddit_velocity: float | None, news_velocity: float | None, st_rank: int | None,
                    wsb_comments: float | None, wsb_sentiment: str | None, call_ratio: float | None,
                    a: dict) -> tuple[float, dict]:
    lo, hi = float(a["velocity_floor"]), float(a["velocity_full"])
    parts = {
        "reddit_velocity": _scaled(reddit_velocity, lo, hi, a["reddit_velocity"]),
        "news_velocity": _scaled(news_velocity, lo, hi, a["news_velocity"]),
        "stocktwits_trending": (float(a["stocktwits_trending"]) * (1 - (int(st_rank) - 1) / 30)
                                if st_rank is not None and not pd.isna(st_rank) and int(st_rank) <= 30 else 0.0),
        "wsb_comments": (0.0 if not wsb_comments or (wsb_sentiment or "").lower() == "bearish"
                         else float(a["wsb_comments"]) * min(math.log1p(wsb_comments) / math.log1p(300), 1.0)),
        "call_volume": _scaled(call_ratio, 0.25, 1.0, a["call_volume"]),
    }
    return float(min(sum(parts.values()), 100.0)), parts


def pump_flags(pennystock_mentions: float, market_cap: float | None, universe_flags: list[str], c: dict) -> list[str]:
    p = c["pump"]
    out = []
    if pennystock_mentions >= float(p["pennystocks_min_mentions"]):
        out.append(f"promoted on r/pennystocks ({pennystock_mentions:.0f} mentions)")
    if market_cap is not None and market_cap < float(p["min_market_cap"]):
        out.append(f"micro cap (${market_cap / 1e6:,.0f}M < ${float(p['min_market_cap']) / 1e6:,.0f}M)")
    out += [f for f in universe_flags if "unavailable" not in f]      # reverse splits / offering filings
    return out


def squeeze_note(sq: dict | None, c: dict) -> str | None:
    if not sq:
        return None
    s = c["squeeze"]
    pct, dtc = sq.get("short_pct_float"), sq.get("days_to_cover")
    if pct is None and dtc is None:
        return None
    hot = (pct or 0) >= float(s["short_pct_float"]) and (dtc or 0) >= float(s["days_to_cover"])
    txt = (f"short interest {pct:.0%} of float" if pct is not None else "short interest n/a") + \
          (f", {dtc:.1f} days to cover" if dtc is not None else "") + \
          (f" (as of {sq.get('short_interest_date')})" if sq.get("short_interest_date") else "")
    return ("squeeze potential: " if hot else "") + txt + "; borrow fee unavailable (no free source)"


def make_plan(df: pd.DataFrame, c: dict, atr: float) -> tuple[HypePlan | None, str | None]:
    p = c["plan"]
    close, low = float(df["close"].iloc[-1]), float(df["low"].iloc[-1])
    z = float(p["entry_zone_pct"])
    lo, hi = close * (1 - z), close * (1 + z)
    atr_stop = close - float(p["stop_atr_mult"]) * atr
    stop, basis = (low, "today's low") if atr_stop < low < lo else (atr_stop, f"close - {p['stop_atr_mult']:g} ATR")
    if stop >= lo or stop <= 0:
        return None, "no room for a stop below the entry zone"
    mid = (lo + hi) / 2
    targets = [mid + float(r) * (mid - stop) for r in p["target_rs"]]
    flags = [f"scale out: half at {p['target_rs'][0]:g}R, rest at {p['target_rs'][-1]:g}R or the "
             f"{p['time_stop_days']}-session time stop"]
    return HypePlan(lo, hi, stop, targets, int(p["time_stop_days"]), basis, flags), None
