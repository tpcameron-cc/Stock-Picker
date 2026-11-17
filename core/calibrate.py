"""Put every skill's score on one scale before ideas from different skills are ranked together.

Each skill's 0-100 score comes from its own point system (catalyst-plays ideas averaged 78 in the first journal
runs, breakout-scanner 60), so raw points aren't comparable across skills. Ranking uses a COMMON score instead:

  common = percentile of the idea's score within ITS OWN skill's past scores      (0-100, "how strong for this skill")
         + skill edge: the skill's realized paper-trade expectancy in R, shrunk     (0 until there's evidence)

Percentile reference = every score that skill has logged to the journal over `lookback_days` (taken or not, so the
reference isn't biased by sizing). With little history the empirical CDF is blended with a prior that is uniform
over the skill's own admissible range [skill min_score, 100], weighted as `prior_weight` pseudo-scores:
  F(x) = (n * ECDF(x) + k * U(x)) / (n + k)
Day one is pure position-in-range; real history takes over as it accumulates. No switch, no manual step.

Skill edge = mean R of the skill's CLOSED journal ideas (taken or not: the journal tracks rejected ideas' outcomes
too), shrunk toward 0 by n / (n + edge_shrink_trades), times `edge_points_per_r`, clipped to +-`edge_max_points`.

The common score only ORDERS ideas (ranking, the primary skill for a ticker, which ideas get the limited slots in
risk-and-sizing). Admission still uses each skill's own scale (merged raw score vs orchestrator.min_score + regime).
Pure functions plus a small `Calibrator` built once per run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from core.config import Settings

SKILL_MIN_SCORE_KEY = {"breakout-scanner": "breakout.scoring.min_score", "catalyst-plays": "catalyst.scoring.min_score",
                       "political-edge": "political.scoring.min_score", "momentum-hype": "hype.min_score"}


def blended_percentile(x: float, ref: np.ndarray, lo: float, k: float) -> float:
    """Percentile (0-100) of `x` among `ref` (mid-rank for ties), blended with a uniform prior on [lo, 100]."""
    hi = 100.0
    u = float(np.clip((x - lo) / (hi - lo), 0.0, 1.0)) if hi > lo else 0.5
    n = len(ref)
    if n == 0:
        return 100.0 * u
    e = (float((ref < x).sum()) + 0.5 * float((ref == x).sum())) / n
    return 100.0 * (n * e + k * u) / (n + k)


def skill_edge(r: pd.Series, shrink: float) -> tuple[float, int, float]:
    """(shrunk mean R, n, raw mean R) from closed ideas' R multiples."""
    x = pd.to_numeric(r, errors="coerce").dropna()
    n = len(x)
    if n == 0:
        return 0.0, 0, float("nan")
    raw = float(x.mean())
    return raw * n / (n + shrink), n, raw


@dataclass
class Calibrator:
    """Built once per run from settings + the journal. `common(skill, score)` -> (common score, parts)."""
    refs: dict[str, np.ndarray] = field(default_factory=dict)
    lows: dict[str, float] = field(default_factory=dict)
    edges: dict[str, dict] = field(default_factory=dict)
    prior_weight: float = 20.0
    points_per_r: float = 25.0
    max_points: float = 15.0
    enabled: bool = True

    @classmethod
    def from_journal(cls, cfg: Settings, trades: pd.DataFrame | None, asof: date) -> "Calibrator":
        c = cfg.section("orchestrator").get("calibration", {}) or {}
        lows = {sk: float(cfg.get(key, 40)) for sk, key in SKILL_MIN_SCORE_KEY.items()}
        refs, edges = {}, {}
        if trades is not None and len(trades):
            t = trades.copy()
            t["_d"] = pd.to_datetime(t["signal_date"]).dt.date
            since = asof - timedelta(days=int(c.get("lookback_days", 365)))
            recent = t[(t["_d"] >= since) & (t["_d"] < asof)]     # past runs only: today's batch doesn't grade itself
            refs = {sk: g["score"].dropna().to_numpy(float) for sk, g in recent.groupby("skill")}
            closed = t[t["status"] == "closed"]
            for sk, g in closed.groupby("skill"):
                shrunk, n, raw = skill_edge(g["r_multiple"], float(c.get("edge_shrink_trades", 30)))
                edges[sk] = {"n": n, "mean_r": raw, "shrunk_r": shrunk}
        return cls(refs=refs, lows=lows, edges=edges, prior_weight=float(c.get("prior_weight", 20)),
                   points_per_r=float(c.get("edge_points_per_r", 25)), max_points=float(c.get("edge_max_points", 15)),
                   enabled=bool(c.get("enabled", True)))

    def common(self, skill: str, score: float) -> tuple[float, dict]:
        if not self.enabled:
            return float(score), {"raw": float(score)}
        ref = self.refs.get(skill, np.array([]))
        pct = blended_percentile(float(score), ref, self.lows.get(skill, 40.0), self.prior_weight)
        e = self.edges.get(skill, {"n": 0, "shrunk_r": 0.0})
        edge = float(np.clip(e["shrunk_r"] * self.points_per_r, -self.max_points, self.max_points))
        return pct + edge, {"raw": float(score), "percentile": round(pct, 1), "history_n": len(ref),
                            "edge_points": round(edge, 1), "edge_trades": e["n"]}

    def describe(self) -> list[str]:
        """One line per skill for the daily report."""
        out = []
        for sk in sorted(set(self.lows) | set(self.refs) | set(self.edges)):
            ref = self.refs.get(sk, np.array([]))
            e = self.edges.get(sk)
            w = len(ref) / (len(ref) + self.prior_weight) if self.prior_weight or len(ref) else 1.0
            med = f", median {np.median(ref):.0f}" if len(ref) else ""
            pts = float(np.clip(e["shrunk_r"] * self.points_per_r, -self.max_points, self.max_points)) if e else 0.0
            edge = (f"edge {pts:+.1f} pts from {e['n']} closed ideas "
                    f"(mean {e['mean_r']:+.2f}R, shrunk {e['shrunk_r']:+.2f}R)" if e else "edge 0 (no closed ideas yet)")
            out.append(f"{sk}: {len(ref)} past scores{med} ({w:.0%} history / {1 - w:.0%} range prior "
                       f"[{self.lows.get(sk, 40):g}-100]); {edge}")
        return out
