"""Small immutable value objects shared by Strength Tracker's pure engines."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional


@dataclass(frozen=True)
class Candle:
    """A completed OHLCV bar. Timestamps are always bar-open UTC timestamps."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Candle timestamp must be timezone-aware UTC")
        if self.timestamp.utcoffset() != timezone.utc.utcoffset(self.timestamp):
            raise ValueError("Candle timestamp must be UTC")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("OHLCV values must be finite")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high must be at least open, close, and low")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low must not exceed open, close, or high")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")


@dataclass(frozen=True)
class CPRLevels:
    pivot: float
    bc: float
    tc: float

    @property
    def bottom(self) -> float:
        return min(self.bc, self.tc)

    @property
    def top(self) -> float:
        return max(self.bc, self.tc)

    @property
    def width(self) -> float:
        return self.top - self.bottom

    @property
    def normalized_width(self) -> float:
        return self.width / abs(self.pivot) if self.pivot else 0.0


@dataclass(frozen=True)
class PivotLevels:
    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float

    def ordered(self) -> tuple[tuple[str, float], ...]:
        return (
            ("S3", self.s3), ("S2", self.s2), ("S1", self.s1),
            ("P", self.pivot),
            ("R1", self.r1), ("R2", self.r2), ("R3", self.r3),
        )


@dataclass(frozen=True)
class MarketStructure:
    symbol: str
    as_of: datetime
    price: float
    session_open: Optional[float]
    active_cpr: CPRLevels
    previous_cpr: CPRLevels
    pivots: PivotLevels
    cpr_width_percentile: Optional[float]
    cpr_regime: str
    atr: Optional[float]
    realized_volatility: Optional[float]
    price_cpr_position: str
    opening_cpr_position: Optional[str]
    pivot_position: str
    level_distances_atr: Mapping[str, Optional[float]]


@dataclass(frozen=True)
class BetaEstimate:
    beta: float
    r_squared: float
    residual_volatility: float
    observations: int
    alpha: float = 0.0
    beta_standard_error: Optional[float] = None
    secondary_benchmark: Optional[str] = None
    secondary_beta: Optional[float] = None
    secondary_beta_standard_error: Optional[float] = None
    secondary_primary_beta: Optional[float] = None
    method: str = "ols"
    effective_observations: Optional[float] = None


@dataclass(frozen=True)
class RelativeStrength:
    symbol: str
    benchmark: str
    as_of: datetime
    score: Optional[float]
    beta: Optional[BetaEstimate]
    horizon_z: Mapping[str, Optional[float]]
    persistence: Optional[float]
    acceleration: Optional[float]
    alignment_ratio: float
    quality_flags: tuple[str, ...] = field(default_factory=tuple)
    reason: Optional[str] = None
    horizon_excess_return: Mapping[str, Optional[float]] = field(default_factory=dict)
    horizon_percentile: Mapping[str, Optional[float]] = field(default_factory=dict)
    model_version: str = "legacy"
    discovery_score: Optional[float] = None

    @property
    def is_usable(self) -> bool:
        return self.score is not None and "stale_or_misaligned" not in self.quality_flags


@dataclass(frozen=True)
class SetupAssessment:
    label: str
    direction: str
    strength: float
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    confirmation: str = "NONE"
    confirmation_price: Optional[float] = None
    discovery_tier: str = "NONE"


@dataclass(frozen=True)
class MarketContext:
    """Research-only market state; none of these fields enter the score.

    ``relative_notional_volume`` is a proxy when derived from candle OHLCV,
    because historical candles expose base volume rather than exact notional.
    """

    source: str = "unknown"
    day_notional_volume: Optional[float] = None
    relative_notional_volume: Optional[float] = None
    impact_spread_bps: Optional[float] = None
    funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    premium: Optional[float] = None
    mark_price: Optional[float] = None
    context_mid_price: Optional[float] = None


@dataclass(frozen=True)
class ScanRow:
    symbol: str
    price: float
    market: MarketStructure
    rs: RelativeStrength
    setup: SetupAssessment
    strong_rank: Optional[int] = None
    weak_rank: Optional[int] = None
    context: Optional[MarketContext] = None
