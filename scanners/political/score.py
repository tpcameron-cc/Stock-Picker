"""Step D scoring: overlap strength first, then agreement among buyers, size, recency and overlays;
penalties for late filing and for the price having already moved since disclosure. Pure functions."""

from __future__ import annotations

import math


def score_purchase(link: str, other_buyers: int, amount_mid: float, days_since_trade: int,
                   filing_delay_days: int, move_since_disclosure: float | None, insider_cluster: bool,
                   sc: dict, lookback_days: int, max_filing_delay: int,
                   member_performance: float = 0.0) -> tuple[float, dict]:
    """`member_performance`: points from screen.member_performance_points (score mode; 0 in gate mode)."""
    parts = {
        "member_performance": float(member_performance),
        "overlap": float(sc.get(link, 0)) if link in ("direct", "sector") else 0.0,
        "agreement": float(min(other_buyers * sc["agreement_per_member"], sc["agreement_max"])),
        "size": 0.0, "recency": 0.0,
        "insider_cluster": float(sc["insider_cluster"]) if insider_cluster else 0.0,
        "late_filing": 0.0, "price_move": 0.0,
    }
    if amount_mid and amount_mid > 1000:
        frac = math.log(amount_mid / 1000) / math.log(float(sc["size_full_usd"]) / 1000)
        parts["size"] = float(sc["size_max"]) * min(max(frac, 0.0), 1.0)
    full = int(sc["recency_full_days"])
    if days_since_trade <= full:
        parts["recency"] = float(sc["recency_max"])
    elif days_since_trade < lookback_days:
        parts["recency"] = float(sc["recency_max"]) * (lookback_days - days_since_trade) / (lookback_days - full)
    if filing_delay_days > max_filing_delay:
        parts["late_filing"] = -float(sc["late_filing_penalty"])
    if move_since_disclosure is not None:
        start = float(sc["move_penalty_start"])
        if move_since_disclosure > start:
            parts["price_move"] = -min(float(sc["move_penalty_max"]),
                                       float(sc["move_penalty_max"]) * (move_since_disclosure - start) / start)
    return float(sum(parts.values())), parts
