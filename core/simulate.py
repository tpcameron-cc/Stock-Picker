"""Shared paper-trade simulator: how a plan (entry zone, stop, target) would have played out on daily bars.

Used by the breakout backtest and by core.journal to mark paper trades, so both apply identical fill rules.
"""

from __future__ import annotations

from typing import Protocol

import numpy as np


class TradePlan(Protocol):
    setup: str
    entry_low: float
    entry_high: float
    stop: float
    target: float
    cancel_below: float | None


def entry_mode(plan) -> str:
    """'buy_stop' (wait for price to trade up through entry_low), 'next_open' (buy the next open in the zone) or
    'limit' (a limit buy at the top of the zone, working for `pending_days` sessions).
    A plan may set `entry_mode` explicitly; otherwise pre-breakouts are buy-stops and everything else next-open."""
    explicit = getattr(plan, "entry_mode", None)
    if explicit in ("buy_stop", "next_open", "limit"):
        return explicit
    return "buy_stop" if plan.setup == "pre_breakout" else "next_open"


# Exit / entry rules. Defaults reproduce the original fixed-target, fixed-stop plan.
DEFAULT_EXITS = {
    "target_mode": "fixed",          # fixed = exit at the plan target; none = no target, let winners run
    "breakeven_at_r": None,          # after the high reaches +X R, raise the stop to the entry price
    "trail_atr_mult": None,          # trail the stop at (highest close since entry - X * ATR)
    "trail_low_days": None,          # trail the stop at the lowest low of the last N bars
    "ma_exit": None,                 # "ema21" | "sma50": exit next open after a close below that average
    "pre_breakout_entry": "buy_stop",  # buy_stop | confirm_close (wait for a close above pivot on volume)
    "confirm_rvol": 1.5,             # volume needed for confirm_close
}


def simulate_trade(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, i: int, plan: TradePlan,
                   pending_days: int, horizon: int, slippage: float = 0.0,
                   same_bar_rule: str = "stop_first", exits: dict | None = None,
                   aux: dict[str, np.ndarray] | None = None,
                   earliest_fill: int | None = None) -> tuple[int, int, float, float, str] | None:
    """Simulate one plan signalled on bar i (decided at bar i's close). Returns
    (fill_idx, exit_idx, fill_px, exit_px, reason) or None if never filled.

    earliest_fill: first bar an order could actually have been placed for (default i + 1). The journal sets it
           to the first session after the idea was LOGGED, so an idea produced after a session never fills on that
           session. Bars between the signal and earliest_fill can only cancel a pending buy-stop (base undercut);
           an entry window (`pending_days`) starts at earliest_fill.

    Entry (see entry_mode): next_open -> next bar's open if it's inside (stop, entry_high];
           limit -> limit buy at entry_high for `pending_days` bars: an open in or below the zone (above the stop)
           fills at the open, a dip from above fills at entry_high; an open at or below the stop cancels;
           buy_stop -> buy-stop at entry_low for `pending_days` bars, fill max(open, entry_low);
           skipped if a bar opens above entry_high (gapped through the zone), cancelled if price undercuts
           `cancel_below` (the base low) before triggering. (Trading below the stop while still pending is
           normal for a buy-stop below the pivot and does not cancel it.)
           buy_stop with exits pre_breakout_entry=confirm_close -> wait for a CLOSE above the pivot with rvol >= confirm_rvol, then
           buy the next open if it's inside (stop, entry_high].
    Exit:  stop (fill at min(open, stop) after the fill bar), target (max(open, target)), or time stop
           at the close of the `horizon`-th bar held. If both are hit in one bar, `same_bar_rule` decides.
           On an intrabar buy-stop fill bar (open below the trigger) the day's low can't be ordered vs the
           fill, so the stop counts only if the bar CLOSES below it (exit at the close).
           Optional (see DEFAULT_EXITS): no target, breakeven stop, ATR / N-day-low trailing stops, MA exit.
           Trailing levels are computed at a bar's close and apply from the NEXT bar (no look-ahead).
    """
    ex = {**DEFAULT_EXITS, **(exits or {})}
    aux = aux or {}
    n = len(c)
    fill_idx = fill_px = None
    intrabar_fill = False
    mode = entry_mode(plan)
    start = max(i + 1, earliest_fill if earliest_fill is not None else i + 1)
    if mode == "buy_stop" and plan.cancel_below is not None:
        if any(l[k] <= plan.cancel_below for k in range(i + 1, min(start, n))):
            return None                                   # base undercut before the order could be placed
    if mode == "next_open":
        j = start
        if j < n and plan.stop < o[j] <= plan.entry_high:
            fill_idx, fill_px = j, o[j]
    elif mode == "limit":
        # Limit buy at entry_high: an open inside the zone fills at the open; an open above it fills at entry_high
        # if the day trades down to it. Price at or below the stop before any fill cancels the order.
        for j in range(start, min(start + pending_days, n)):
            if o[j] <= plan.stop:
                return None
            if o[j] <= plan.entry_high:
                fill_idx, fill_px = j, o[j]
                break
            if l[j] <= plan.entry_high:
                fill_idx, fill_px = j, plan.entry_high
                break
    elif ex["pre_breakout_entry"] == "confirm_close":
        rv = aux["rvol"]
        for j in range(start, min(start + pending_days, n)):
            if plan.cancel_below is not None and l[j] <= plan.cancel_below:
                return None
            if c[j] > plan.entry_low and rv[j] >= float(ex["confirm_rvol"]):
                if j + 1 < n and plan.stop < o[j + 1] <= plan.entry_high:
                    fill_idx, fill_px = j + 1, o[j + 1]
                break
    else:
        for j in range(start, min(start + pending_days, n)):
            if o[j] > plan.entry_high:
                return None
            if h[j] >= plan.entry_low:
                fill_idx, fill_px = j, max(o[j], plan.entry_low)
                intrabar_fill = o[j] < plan.entry_low
                break
            if plan.cancel_below is not None and l[j] <= plan.cancel_below:
                return None
    if fill_idx is None:
        return None
    raw_fill = fill_px
    fill_px *= 1 + slippage

    target = plan.target if ex["target_mode"] == "fixed" else float("inf")
    stop = plan.stop
    risk0 = raw_fill - plan.stop
    peak = raw_fill

    def stop_reason() -> str:
        return "stop" if stop <= plan.stop else "trail_stop"

    last = min(fill_idx + horizon - 1, n - 1)
    for k in range(fill_idx, last + 1):
        stop_hit, tgt_hit = l[k] <= stop, h[k] >= target
        if k == fill_idx and intrabar_fill:
            stop_hit = c[k] < stop
            if stop_hit and not tgt_hit:
                return fill_idx, k, fill_px, c[k] * (1 - slippage), stop_reason()
        if k == fill_idx:
            stop_px, tgt_px = stop, target        # intrabar after the fill: no gap through
        else:
            stop_px, tgt_px = min(o[k], stop), max(o[k], target)
        if stop_hit and tgt_hit:
            if same_bar_rule == "stop_first":
                return fill_idx, k, fill_px, stop_px * (1 - slippage), stop_reason()
            return fill_idx, k, fill_px, tgt_px * (1 - slippage), "target"
        if stop_hit:
            return fill_idx, k, fill_px, stop_px * (1 - slippage), stop_reason()
        if tgt_hit:
            return fill_idx, k, fill_px, tgt_px * (1 - slippage), "target"
        if k == last:
            break
        # End of bar k: close-based exit and stop updates take effect from bar k+1.
        if ex["ma_exit"] and c[k] < aux[ex["ma_exit"]][k]:
            return fill_idx, k + 1, fill_px, o[k + 1] * (1 - slippage), "ma_exit"
        if ex["breakeven_at_r"] is not None and h[k] >= raw_fill + float(ex["breakeven_at_r"]) * risk0:
            stop = max(stop, raw_fill)
        peak = max(peak, c[k])
        if ex["trail_atr_mult"] is not None:
            stop = max(stop, peak - float(ex["trail_atr_mult"]) * aux["atr"][k])
        if ex["trail_low_days"] is not None:
            stop = max(stop, aux[f"low{int(ex['trail_low_days'])}"][k])
    reason = "time" if last == fill_idx + horizon - 1 else "open_at_end"
    return fill_idx, last, fill_px, c[last] * (1 - slippage), reason
