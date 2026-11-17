"""Market regime check: trend, breadth and volatility -> strong / neutral / weak, and what that changes.

    uv run python -m core.regime

Seven bullish checks (one point each): SPY above its 50-day, above its 200-day, 50-day rising; sector breadth
(share of the 11 SPDR sector ETFs above their 50-day) >= threshold; equal-weight S&P keeping up with SPY;
VIX below the calm level; VIX not rising vs its 20-day average.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from core import indicators as ind
from core.config import Settings, settings as default_settings


@dataclass
class Regime:
    label: str                       # strong | neutral | weak
    points: int
    max_points: int
    checks: dict[str, bool | None]   # None = data unavailable (counts as not bullish)
    detail: dict[str, float | None]
    min_score_add: float
    size_multiplier: float
    as_of: date | None
    notes: list[str] = field(default_factory=list)

    def headline(self) -> str:
        passed = [k for k, v in self.checks.items() if v]
        failed = [k for k, v in self.checks.items() if v is False]
        missing = [k for k, v in self.checks.items() if v is None]
        s = (f"Market regime: {self.label.upper()} ({self.points}/{self.max_points} bullish checks, as of {self.as_of}). ")
        if self.label != "strong":
            s += (f"Score thresholds raised by {self.min_score_add:g} and position sizes cut to "
                  f"{self.size_multiplier:.0%}. ")
        s += f"Passing: {', '.join(passed) or 'none'}. Failing: {', '.join(failed) or 'none'}."
        if missing:
            s += f" Unavailable: {', '.join(missing)}."
        return s


def evaluate_regime(spy: pd.Series, vix: pd.Series | None, sectors: dict[str, pd.Series],
                    equal_weight: pd.Series | None, r: dict) -> Regime:
    """Pure function over close series."""
    d: dict[str, float | None] = {}
    checks: dict[str, bool | None] = {}
    sma50, sma200 = ind.sma(spy, 50), ind.sma(spy, 200)
    last = float(spy.iloc[-1])
    d.update(spy=last, spy_sma50=_f(sma50.iloc[-1]), spy_sma200=_f(sma200.iloc[-1]))
    checks["SPY above 50-day"] = _cmp(last, sma50.iloc[-1])
    checks["SPY above 200-day"] = _cmp(last, sma200.iloc[-1])
    lb = int(r["slope_lookback"])
    checks["SPY 50-day rising"] = _cmp(sma50.iloc[-1], sma50.iloc[-1 - lb]) if len(sma50) > lb else None

    above = [float(s.iloc[-1]) > float(ind.sma(s, 50).iloc[-1]) for s in sectors.values()
             if len(s) >= 50 and pd.notna(ind.sma(s, 50).iloc[-1])]
    d["breadth"] = sum(above) / len(above) if above else None
    d["sectors_counted"] = len(above)
    checks["sector breadth"] = (d["breadth"] >= float(r["breadth_min"])) if above else None

    if equal_weight is not None and len(equal_weight) > int(r["equal_weight_lookback"]):
        a, b = equal_weight.align(spy, join="inner")
        ratio = a / b
        n = int(r["equal_weight_lookback"])
        d["equal_weight_vs_spy"] = float(ratio.iloc[-1] / ratio.iloc[-1 - n] - 1)
        checks["equal-weight keeping up"] = d["equal_weight_vs_spy"] >= 0
    else:
        checks["equal-weight keeping up"] = None

    if vix is not None and len(vix) >= 20:
        v, v20 = float(vix.iloc[-1]), float(ind.sma(vix, 20).iloc[-1])
        d.update(vix=v, vix_sma20=v20)
        checks["VIX calm"] = v < float(r["vix_calm"])
        checks["VIX not rising"] = v <= v20 * float(r["vix_rising_ratio"])
    else:
        checks["VIX calm"] = checks["VIX not rising"] = None

    pts = sum(1 for v in checks.values() if v)
    label = "strong" if pts >= int(r["strong_min_points"]) else "weak" if pts <= int(r["weak_max_points"]) else "neutral"
    act = r["actions"][label]
    return Regime(label, pts, len(checks), checks, d, float(act["min_score_add"]), float(act["size_multiplier"]),
                  spy.index[-1].date() if len(spy) else None)


def _f(x) -> float | None:
    return None if pd.isna(x) else float(x)


def _cmp(a, b) -> bool | None:
    return None if pd.isna(a) or pd.isna(b) else bool(a > b)


def current_regime(cfg: Settings | None = None, price_provider=None) -> Regime:
    from core.providers.yfinance_provider import YFinanceProvider

    cfg = cfg or default_settings()
    r = cfg.section("regime")
    prov = price_provider or YFinanceProvider(cfg)
    bench = cfg.get("indicators.benchmark")
    syms = [bench, r["vix"], r["equal_weight"], *r["sector_etfs"]]
    hist = prov.history(syms, "2y")
    if bench not in hist:
        raise RuntimeError(f"{bench} prices unavailable; cannot assess the market regime")
    close = {k: v["close"] for k, v in hist.items()}
    reg = evaluate_regime(close[bench], close.get(r["vix"].upper()), {s: close[s] for s in r["sector_etfs"] if s in close},
                          close.get(r["equal_weight"]), r)
    missing = [s for s in syms if s.upper() not in close]
    if missing:
        reg.notes.append(f"no prices for {', '.join(missing)}")
    dates = {k: v.index[-1].date() for k, v in close.items()}
    if len(set(dates.values())) > 1:
        reg.notes.append("components have different latest bars (provider hasn't published all of today's data): "
                         + ", ".join(f"{k} {d}" for k, d in sorted(dates.items(), key=lambda x: x[1])))
    return reg


def main() -> None:
    reg = current_regime()
    print(reg.headline())
    for k, v in reg.detail.items():
        print(f"  {k}: {v if v is None else round(v, 4)}")
    for n in reg.notes:
        print("  note:", n)


if __name__ == "__main__":
    main()
