"""The standard Signal schema every scanning skill outputs, plus JSON/markdown writers."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


class Confidence(str, Enum):
    low = "low"
    med = "med"
    high = "high"


class Source(BaseModel):
    """A citation: where a figure came from and how fresh it is."""

    url: HttpUrl
    as_of: date
    note: str | None = None


class PriceZone(BaseModel):
    low: float = Field(gt=0)
    high: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "PriceZone":
        if self.low > self.high:
            raise ValueError(f"entry zone low {self.low} > high {self.high}")
        return self


class KeyDate(BaseModel):
    date: date
    kind: str  # e.g. earnings, pdufa, hearing, markup, lockup, index_add
    note: str | None = None


class Signal(BaseModel):
    """One stock idea. Research and trade plan only; never an order."""

    ticker: str = Field(min_length=1, max_length=10)
    company: str
    skill: str
    score: float = Field(ge=0, le=100)
    confidence: Confidence
    thesis: str = Field(min_length=1)
    signals_hit: list[str] = Field(default_factory=list)
    entry_zone: PriceZone
    stop: float = Field(gt=0)
    targets: list[float] = Field(min_length=1)
    horizon_days: int = Field(gt=0)
    key_dates: list[KeyDate] = Field(default_factory=list)
    invalidation: str = Field(min_length=1)
    risk_flags: list[str] = Field(default_factory=list)
    sources: list[Source] = Field(min_length=1)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _long_plan_is_coherent(self) -> "Signal":
        # Long-only ideas: stop below entry, every target above entry.
        if self.stop >= self.entry_zone.low:
            raise ValueError(f"{self.ticker}: stop {self.stop} must be below entry zone low {self.entry_zone.low}")
        bad = [t for t in self.targets if t <= self.entry_zone.high]
        if bad:
            raise ValueError(f"{self.ticker}: targets {bad} must be above entry zone high {self.entry_zone.high}")
        return self

    @property
    def entry_mid(self) -> float:
        return (self.entry_zone.low + self.entry_zone.high) / 2

    @property
    def reward_risk(self) -> float:
        """Reward:risk to the first target from the middle of the entry zone."""
        risk = self.entry_mid - self.stop
        return (self.targets[0] - self.entry_mid) / risk


def save_signals(signals: Iterable[Signal], out_dir: str | Path, stem: str) -> tuple[Path, Path]:
    """Write `<stem>.json` and `<stem>.md` into out_dir. Returns both paths."""
    sigs = list(signals)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{stem}.json"
    md_path = out / f"{stem}.md"
    json_path.write_text(
        json.dumps([s.model_dump(mode="json") for s in sigs], indent=2), encoding="utf-8"
    )
    md_path.write_text(signals_to_markdown(sigs), encoding="utf-8")
    return json_path, md_path


def load_signals(path: str | Path) -> list[Signal]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Signal.model_validate(d) for d in data]


def signals_to_markdown(signals: list[Signal]) -> str:
    if not signals:
        return "_No signals._\n"
    lines: list[str] = []
    for s in sorted(signals, key=lambda x: x.score, reverse=True):
        targets = ", ".join(f"{t:.2f}" for t in s.targets)
        lines += [
            f"### {s.ticker} — {s.company}  ({s.skill}, score {s.score:.0f}, {s.confidence.value} confidence)",
            "",
            s.thesis,
            "",
            f"- **Plan:** entry {s.entry_zone.low:.2f}–{s.entry_zone.high:.2f}, stop {s.stop:.2f}, "
            f"target(s) {targets}, horizon {s.horizon_days}d, R:R {s.reward_risk:.1f}",
            f"- **Signals:** {', '.join(s.signals_hit) or '—'}",
            f"- **Invalidation:** {s.invalidation}",
        ]
        if s.key_dates:
            lines.append("- **Key dates:** " + "; ".join(f"{k.date} {k.kind}" for k in s.key_dates))
        if s.risk_flags:
            lines.append("- **Risk flags:** " + ", ".join(s.risk_flags))
        lines.append("- **Sources:** " + "; ".join(f"[{src.url.host}]({src.url}) as of {src.as_of}" for src in s.sources))
        lines.append("")
    return "\n".join(lines)
