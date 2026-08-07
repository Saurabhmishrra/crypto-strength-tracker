"""Persistent, beta-adjusted relative strength versus a BTC benchmark."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence

from .models import BetaEstimate, Candle, RelativeStrength


@dataclass(frozen=True)
class RSConfig:
    """All windows are numbers of completed bars, normally one-hour bars."""

    beta_window: int = 720
    min_beta_points: int = 240
    persistence_window: int = 24
    min_alignment_ratio: float = 0.95
    short_horizon_bars: int = 4
    medium_horizon_bars: int = 24
    long_horizon_bars: int = 168


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(current / previous) for previous, current in zip(closes, closes[1:])]


def estimate_beta(alt_returns: Sequence[float], btc_returns: Sequence[float], min_points: int) -> Optional[BetaEstimate]:
    """OLS beta with intercept, R², and residual volatility.

    A beta is a hedge estimate, not a causal model. The reported R² and sample
    count are deliberately retained so callers cannot mistake a noisy slope for
    a precise beta.
    """
    n = min(len(alt_returns), len(btc_returns))
    if n < min_points:
        return None
    alt = list(alt_returns[-n:])
    btc = list(btc_returns[-n:])
    mean_alt = statistics.fmean(alt)
    mean_btc = statistics.fmean(btc)
    ss_btc = sum((value - mean_btc) ** 2 for value in btc)
    if ss_btc <= 1e-18:
        return None
    beta = sum((b - mean_btc) * (a - mean_alt) for a, b in zip(alt, btc)) / ss_btc
    alpha = mean_alt - beta * mean_btc
    residuals = [a - (alpha + beta * b) for a, b in zip(alt, btc)]
    ss_residual = sum(value * value for value in residuals)
    ss_total = sum((value - mean_alt) ** 2 for value in alt)
    r_squared = max(0.0, 1.0 - ss_residual / ss_total) if ss_total > 1e-18 else 0.0
    residual_volatility = statistics.pstdev(residuals)
    return BetaEstimate(beta=beta, r_squared=r_squared, residual_volatility=residual_volatility, observations=n)


def _contiguous_aligned_closes(
    asset: Sequence[Candle], benchmark: Sequence[Candle], interval_seconds: int
) -> tuple[list[datetime], list[float], list[float], float]:
    """Exact timestamp alignment only; return the most recent contiguous suffix.

    Forward-filling a benchmark or an asset would manufacture relative returns.
    A gap therefore ends the usable suffix and is surfaced through alignment
    quality rather than silently patched.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    by_asset = {c.timestamp: c.close for c in asset}
    by_benchmark = {c.timestamp: c.close for c in benchmark}
    common = sorted(set(by_asset).intersection(by_benchmark))
    denominator = max(1, max(len(by_asset), len(by_benchmark)))
    alignment_ratio = len(common) / denominator
    if not common:
        return [], [], [], alignment_ratio
    start = len(common) - 1
    while start > 0 and (common[start] - common[start - 1]).total_seconds() == interval_seconds:
        start -= 1
    timestamps = common[start:]
    return timestamps, [by_asset[ts] for ts in timestamps], [by_benchmark[ts] for ts in timestamps], alignment_ratio


def _horizon_z(asset_closes: Sequence[float], btc_closes: Sequence[float], beta: BetaEstimate, bars: int) -> Optional[float]:
    if bars <= 0 or len(asset_closes) <= bars or len(btc_closes) <= bars:
        return None
    residual_return = math.log(asset_closes[-1] / asset_closes[-1 - bars]) - beta.beta * math.log(btc_closes[-1] / btc_closes[-1 - bars])
    scale = beta.residual_volatility * math.sqrt(bars)
    if scale <= 1e-12:
        return 0.0 if abs(residual_return) <= 1e-12 else None
    return residual_return / scale


def _persistence(residuals: Sequence[float], window: int) -> Optional[float]:
    values = [r for r in residuals[-window:] if abs(r) > 1e-15]
    if len(values) < max(4, window // 3):
        return None
    return (sum(value > 0 for value in values) - sum(value < 0 for value in values)) / len(values)


def _score(horizons: Mapping[str, Optional[float]], persistence: Optional[float], acceleration: Optional[float]) -> Optional[float]:
    if any(horizons.get(name) is None for name in ("short", "medium", "long")) or persistence is None or acceleration is None:
        return None
    norm = lambda value: _clamp(float(value) / 3.0, -1.0, 1.0)
    composite = (
        0.15 * norm(horizons["short"])
        + 0.35 * norm(horizons["medium"])
        + 0.30 * norm(horizons["long"])
        + 0.15 * _clamp(persistence, -1.0, 1.0)
        + 0.05 * norm(acceleration)
    )
    return round(_clamp(10.0 * composite, -10.0, 10.0), 2)


def compute_relative_strength(
    symbol: str,
    asset_bars: Sequence[Candle],
    benchmark: str,
    benchmark_bars: Sequence[Candle],
    interval_seconds: int,
    as_of: datetime,
    config: RSConfig = RSConfig(),
) -> RelativeStrength:
    """Compute a quality-aware, multi-horizon beta-adjusted RS score.

    The as-of bar is included because the score is known only after that bar
    closes. No later observation is used. Callers must pass closed bars only.
    """
    timestamps, asset_close, btc_close, alignment_ratio = _contiguous_aligned_closes(
        asset_bars, benchmark_bars, interval_seconds
    )
    # A newer listing can be scored after the stated minimum, but its shorter
    # beta sample remains visible through ``beta.observations``. Requiring the
    # entire preferred window here would make ``min_beta_points`` a lying knob.
    required_bars = max(config.min_beta_points, config.long_horizon_bars) + 1
    base_flags: list[str] = []
    if alignment_ratio < config.min_alignment_ratio:
        base_flags.append("stale_or_misaligned")
    if timestamps and as_of - (timestamps[-1] + timedelta(seconds=interval_seconds)) > timedelta(seconds=interval_seconds):
        base_flags.append("stale_or_misaligned")
    if len(asset_close) < required_bars:
        if "stale_or_misaligned" not in base_flags:
            base_flags.append("stale_or_misaligned")
        return RelativeStrength(
            symbol=symbol, benchmark=benchmark, as_of=as_of, score=None, beta=None,
            horizon_z={}, persistence=None, acceleration=None, alignment_ratio=alignment_ratio,
            quality_flags=tuple(base_flags), reason=f"need {required_bars} contiguous aligned bars; got {len(asset_close)}",
        )

    asset_returns = _log_returns(asset_close)
    btc_returns = _log_returns(btc_close)
    beta = estimate_beta(asset_returns[-config.beta_window:], btc_returns[-config.beta_window:], config.min_beta_points)
    if beta is None:
        return RelativeStrength(
            symbol=symbol, benchmark=benchmark, as_of=as_of, score=None, beta=None,
            horizon_z={}, persistence=None, acceleration=None, alignment_ratio=alignment_ratio,
            quality_flags=tuple(base_flags), reason="unable to estimate beta from valid benchmark returns",
        )

    residuals = [asset_return - beta.beta * btc_return for asset_return, btc_return in zip(asset_returns, btc_returns)]
    horizons = {
        "short": _horizon_z(asset_close, btc_close, beta, config.short_horizon_bars),
        "medium": _horizon_z(asset_close, btc_close, beta, config.medium_horizon_bars),
        "long": _horizon_z(asset_close, btc_close, beta, config.long_horizon_bars),
    }
    persistence = _persistence(residuals, config.persistence_window)
    acceleration = None
    if horizons["short"] is not None and horizons["medium"] is not None:
        acceleration = horizons["short"] - horizons["medium"]

    flags = base_flags
    if beta.r_squared < 0.05:
        flags.append("low_beta_fit")
    if beta.residual_volatility <= 1e-12:
        flags.append("near_zero_residual_volatility")
    score = _score(horizons, persistence, acceleration)
    reason = None if score is not None else "insufficient usable variation for multi-horizon score"
    return RelativeStrength(
        symbol=symbol, benchmark=benchmark, as_of=as_of, score=score, beta=beta,
        horizon_z=horizons, persistence=persistence, acceleration=acceleration,
        alignment_ratio=alignment_ratio, quality_flags=tuple(flags), reason=reason,
    )
