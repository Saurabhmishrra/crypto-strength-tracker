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
    bar_interval_seconds: int = 3600
    beta_half_life_bars: int = 168
    min_empirical_windows: int = 60
    winsor_mad: float = 6.0
    secondary_benchmark: Optional[str] = "ETH"

    @classmethod
    def for_interval(cls, interval_seconds: int) -> "RSConfig":
        """Keep the economic horizons fixed when the candle interval changes."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")

        def bars(seconds: int) -> int:
            return max(1, math.ceil(seconds / interval_seconds))

        return cls(
            beta_window=bars(30 * 86_400),
            min_beta_points=bars(10 * 86_400),
            persistence_window=bars(86_400),
            short_horizon_bars=bars(4 * 3_600),
            medium_horizon_bars=bars(86_400),
            long_horizon_bars=bars(7 * 86_400),
            bar_interval_seconds=interval_seconds,
            beta_half_life_bars=bars(7 * 86_400),
        )

    def __post_init__(self) -> None:
        windows = {
            "beta_window": self.beta_window,
            "min_beta_points": self.min_beta_points,
            "persistence_window": self.persistence_window,
            "short_horizon_bars": self.short_horizon_bars,
            "medium_horizon_bars": self.medium_horizon_bars,
            "long_horizon_bars": self.long_horizon_bars,
            "bar_interval_seconds": self.bar_interval_seconds,
            "beta_half_life_bars": self.beta_half_life_bars,
            "min_empirical_windows": self.min_empirical_windows,
        }
        non_positive = [name for name, value in windows.items() if value <= 0]
        if non_positive:
            raise ValueError(f"RS windows must be positive: {', '.join(non_positive)}")
        if self.beta_window < self.min_beta_points:
            raise ValueError("beta_window must be at least min_beta_points")
        if not (
            self.short_horizon_bars
            < self.medium_horizon_bars
            < self.long_horizon_bars
        ):
            raise ValueError("RS horizons must be strictly ordered short < medium < long")
        if not 0.0 < self.min_alignment_ratio <= 1.0:
            raise ValueError("min_alignment_ratio must be in (0, 1]")
        if self.winsor_mad <= 0:
            raise ValueError("winsor_mad must be positive")


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(value, upper))


def _log_returns(closes: Sequence[float]) -> list[float]:
    return [math.log(current / previous) for previous, current in zip(closes, closes[1:])]


def _robust_location_scale(values: Sequence[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    location = statistics.median(values)
    mad = statistics.median(abs(value - location) for value in values)
    scale = 1.4826 * mad
    if scale <= 1e-12 and len(values) >= 2:
        scale = statistics.pstdev(values)
    return location, scale


def _winsorize(values: Sequence[float], limit: float) -> list[float]:
    location, scale = _robust_location_scale(values)
    if scale <= 1e-12:
        return list(values)
    lower, upper = location - limit * scale, location + limit * scale
    return [_clamp(value, lower, upper) for value in values]


def _ewma_weights(length: int, half_life: int) -> list[float]:
    if length <= 0 or half_life <= 0:
        return []
    raw = [0.5 ** ((length - 1 - index) / half_life) for index in range(length)]
    total = sum(raw)
    return [value / total for value in raw]


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    return sum(value * weight for value, weight in zip(values, weights))


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
    beta_standard_error = None
    if n > 2 and ss_btc > 1e-18:
        beta_standard_error = math.sqrt((ss_residual / (n - 2)) / ss_btc)
    return BetaEstimate(
        beta=beta,
        r_squared=r_squared,
        residual_volatility=residual_volatility,
        observations=n,
        alpha=alpha,
        beta_standard_error=beta_standard_error,
        method="ols",
        effective_observations=float(n),
    )


def estimate_robust_ewma_factor_model(
    alt_returns: Sequence[float],
    btc_returns: Sequence[float],
    min_points: int,
    half_life: int,
    winsor_mad: float,
    secondary_returns: Optional[Sequence[float]] = None,
    secondary_benchmark: Optional[str] = None,
    secondary_primary_beta: Optional[float] = None,
) -> Optional[BetaEstimate]:
    """Robust exponentially weighted one- or two-factor regression.

    Inputs are winsorised for coefficient estimation, while diagnostics are
    calculated on the original observations so tail risk remains visible.
    The optional second factor should already be orthogonalised to BTC.
    """
    lengths = [len(alt_returns), len(btc_returns)]
    if secondary_returns is not None:
        lengths.append(len(secondary_returns))
    n = min(lengths)
    if n < min_points:
        return None
    alt_raw = list(alt_returns[-n:])
    btc_raw = list(btc_returns[-n:])
    secondary_raw = list(secondary_returns[-n:]) if secondary_returns is not None else None
    alt = _winsorize(alt_raw, winsor_mad)
    btc = _winsorize(btc_raw, winsor_mad)
    secondary = _winsorize(secondary_raw, winsor_mad) if secondary_raw is not None else None
    weights = _ewma_weights(n, half_life)
    effective_n = 1.0 / sum(weight * weight for weight in weights)

    mean_alt = _weighted_mean(alt, weights)
    mean_btc = _weighted_mean(btc, weights)
    centered_alt = [value - mean_alt for value in alt]
    centered_btc = [value - mean_btc for value in btc]
    var_btc = _weighted_mean([value * value for value in centered_btc], weights)
    if var_btc <= 1e-18:
        return None

    secondary_beta = None
    secondary_standard_error = None
    if secondary is None:
        beta = _weighted_mean(
            [x * y for x, y in zip(centered_btc, centered_alt)], weights
        ) / var_btc
        alpha = mean_alt - beta * mean_btc
    else:
        mean_secondary = _weighted_mean(secondary, weights)
        centered_secondary = [value - mean_secondary for value in secondary]
        var_secondary = _weighted_mean(
            [value * value for value in centered_secondary], weights
        )
        covariance = _weighted_mean(
            [x * z for x, z in zip(centered_btc, centered_secondary)], weights
        )
        cov_alt_btc = _weighted_mean(
            [x * y for x, y in zip(centered_btc, centered_alt)], weights
        )
        cov_alt_secondary = _weighted_mean(
            [z * y for z, y in zip(centered_secondary, centered_alt)], weights
        )
        determinant = var_btc * var_secondary - covariance * covariance
        if determinant <= 1e-18:
            return estimate_robust_ewma_factor_model(
                alt_returns, btc_returns, min_points, half_life, winsor_mad
            )
        beta = (
            cov_alt_btc * var_secondary - cov_alt_secondary * covariance
        ) / determinant
        secondary_beta = (
            cov_alt_secondary * var_btc - cov_alt_btc * covariance
        ) / determinant
        alpha = mean_alt - beta * mean_btc - secondary_beta * mean_secondary

    residuals = []
    for index, (alt_value, btc_value) in enumerate(zip(alt_raw, btc_raw)):
        predicted = alpha + beta * btc_value
        if secondary_raw is not None and secondary_beta is not None:
            predicted += secondary_beta * secondary_raw[index]
        residuals.append(alt_value - predicted)
    residual_for_scale = _winsorize(residuals, winsor_mad)
    residual_mean = _weighted_mean(residual_for_scale, weights)
    residual_variance = _weighted_mean(
        [(value - residual_mean) ** 2 for value in residual_for_scale], weights
    )
    residual_volatility = math.sqrt(max(0.0, residual_variance))
    raw_mean = _weighted_mean(alt_raw, weights)
    total_variance = _weighted_mean(
        [(value - raw_mean) ** 2 for value in alt_raw], weights
    )
    raw_residual_variance = _weighted_mean(
        [value * value for value in residuals], weights
    )
    r_squared = (
        _clamp(1.0 - raw_residual_variance / total_variance, 0.0, 1.0)
        if total_variance > 1e-18 else 0.0
    )
    if secondary is not None and secondary_beta is not None:
        mean_secondary = _weighted_mean(secondary, weights)
        var_secondary = _weighted_mean(
            [(value - mean_secondary) ** 2 for value in secondary], weights
        )
        # Diagonal of the inverse weighted factor covariance matrix. Ignoring
        # the off-diagonal term would understate uncertainty when the nominally
        # orthogonal secondary factor still has sample correlation with BTC.
        beta_standard_error = math.sqrt(
            residual_variance * var_secondary
            / max(effective_n * determinant, 1e-18)
        )
        secondary_standard_error = math.sqrt(
            residual_variance * var_btc
            / max(effective_n * determinant, 1e-18)
        )
    else:
        beta_standard_error = math.sqrt(
            residual_variance / max(effective_n * var_btc, 1e-18)
        )
    return BetaEstimate(
        beta=beta,
        r_squared=r_squared,
        residual_volatility=residual_volatility,
        observations=n,
        alpha=alpha,
        beta_standard_error=beta_standard_error,
        secondary_benchmark=secondary_benchmark if secondary_beta is not None else None,
        secondary_beta=secondary_beta,
        secondary_beta_standard_error=secondary_standard_error,
        secondary_primary_beta=(
            secondary_primary_beta if secondary_beta is not None else None
        ),
        method="robust_ewma_two_factor" if secondary_beta is not None else "robust_ewma_btc",
        effective_observations=effective_n,
    )


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
    if len(by_asset) != len(asset) or len(by_benchmark) != len(benchmark):
        raise ValueError("duplicate timestamps are not safe for relative-strength alignment")
    if not by_asset or not by_benchmark:
        return [], [], [], 0.0

    # Judge completeness only over the period both markets existed. Comparing a
    # new listing's 300 perfectly aligned bars with BTC's full 720-bar cache
    # would call listing age a data fault and make min_beta_points ineffective.
    overlap_start = max(min(by_asset), min(by_benchmark))
    overlap_end = min(max(by_asset), max(by_benchmark))
    if overlap_start > overlap_end:
        return [], [], [], 0.0
    asset_overlap = {ts for ts in by_asset if overlap_start <= ts <= overlap_end}
    benchmark_overlap = {ts for ts in by_benchmark if overlap_start <= ts <= overlap_end}
    common = sorted(asset_overlap.intersection(benchmark_overlap))
    expected = int((overlap_end - overlap_start).total_seconds() // interval_seconds) + 1
    denominator = max(1, expected, len(asset_overlap), len(benchmark_overlap))
    alignment_ratio = len(common) / denominator
    if not common:
        return [], [], [], alignment_ratio
    start = len(common) - 1
    while start > 0 and (common[start] - common[start - 1]).total_seconds() == interval_seconds:
        start -= 1
    timestamps = common[start:]
    return (
        timestamps,
        [by_asset[ts] for ts in timestamps],
        [by_benchmark[ts] for ts in timestamps],
        alignment_ratio,
    )


def _rolling_sums(values: Sequence[float], window: int) -> list[float]:
    if window <= 0 or len(values) < window:
        return []
    total = sum(values[:window])
    output = [total]
    for index in range(window, len(values)):
        total += values[index] - values[index - window]
        output.append(total)
    return output


def _empirical_horizon(
    factor_adjusted_returns: Sequence[float],
    bars: int,
    min_windows: int,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Current horizon excess return, robust historical z, and percentile.

    The historical distribution excludes the current window. Its overlapping
    samples make this a descriptive standardisation, not an independent-sample
    p-value; unlike ``sigma * sqrt(h)``, it still reflects observed volatility
    clustering, autocorrelation, and fat tails at the requested horizon.
    """
    samples = _rolling_sums(factor_adjusted_returns, bars)
    if len(samples) < min_windows + 1:
        return None, None, None
    current = samples[-1]
    history = samples[:-1]
    _location, scale = _robust_location_scale(history)
    if scale <= 1e-12:
        z_score = 0.0 if abs(current) <= 1e-12 else None
    else:
        # Zero is the factor-neutral null. Historical location is deliberately
        # not subtracted: a token with steady positive alpha should remain
        # strong rather than have its leadership defined away. The empirical
        # percentile below separately answers whether the move is unusual for
        # this token's own history.
        z_score = current / scale
    below = sum(value < current for value in history)
    equal = sum(value == current for value in history)
    percentile = (below + 0.5 * equal) / len(history)
    return z_score, current, percentile


def _persistence(residuals: Sequence[float], window: int) -> Optional[float]:
    values = [r for r in residuals[-window:] if abs(r) > 1e-15]
    if len(values) < max(4, window // 3):
        return None
    return (sum(value > 0 for value in values) - sum(value < 0 for value in values)) / len(values)


def _acceleration(
    residuals: Sequence[float], short_window: int, medium_window: int, residual_volatility: float
) -> Optional[float]:
    """Standardised shift in recent factor-adjusted return versus the prior window.

    Comparing a short cumulative z-score directly with a longer cumulative
    z-score is not acceleration: under a perfectly steady excess return, the
    longer value grows with ``sqrt(window)`` and mechanically makes the result
    negative. Non-overlapping sample means put both periods in the same units.
    """
    if short_window <= 0 or medium_window <= short_window or len(residuals) < medium_window:
        return None
    recent = residuals[-short_window:]
    prior = residuals[-medium_window:-short_window]
    difference = statistics.fmean(recent) - statistics.fmean(prior)
    scale = residual_volatility * math.sqrt(1.0 / len(recent) + 1.0 / len(prior))
    if scale <= 1e-12:
        return 0.0 if abs(difference) <= 1e-12 else None
    return difference / scale


def _score(
    horizons: Mapping[str, Optional[float]],
    persistence: Optional[float],
    acceleration: Optional[float],
) -> Optional[float]:
    if (
        any(horizons.get(name) is None for name in ("short", "medium", "long"))
        or persistence is None
        or acceleration is None
    ):
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


def _discovery_score(
    horizons: Mapping[str, Optional[float]],
    acceleration: Optional[float],
) -> Optional[float]:
    """Persistence-free score for early research discovery.

    The confirmed-candidate score above is frozen for the original H1 rule.
    Discovery deliberately excludes persistence because persistence already acts
    as a hard confirmation gate and has not shown predictive value in replay.
    The remaining frozen weights are rescaled from 0.85 to 1.00 so the output
    keeps the familiar -10 to +10 range without changing their relative weights.
    """
    if (
        any(horizons.get(name) is None for name in ("short", "medium", "long"))
        or acceleration is None
    ):
        return None
    norm = lambda value: _clamp(float(value) / 3.0, -1.0, 1.0)
    composite = (
        0.15 * norm(horizons["short"])
        + 0.35 * norm(horizons["medium"])
        + 0.30 * norm(horizons["long"])
        + 0.05 * norm(acceleration)
    ) / 0.85
    return round(_clamp(10.0 * composite, -10.0, 10.0), 2)


def compute_relative_strength(
    symbol: str,
    asset_bars: Sequence[Candle],
    benchmark: str,
    benchmark_bars: Sequence[Candle],
    interval_seconds: int,
    as_of: datetime,
    config: RSConfig = RSConfig(),
    secondary_benchmark: Optional[str] = None,
    secondary_bars: Sequence[Candle] = (),
) -> RelativeStrength:
    """Compute a quality-aware, multi-horizon beta-adjusted RS score.

    The as-of bar is included because the score is known only after that bar
    closes. No later observation is used. Callers must pass closed bars only.
    """
    if interval_seconds != config.bar_interval_seconds:
        raise ValueError("interval_seconds must match RSConfig bar_interval_seconds")
    timestamps, asset_close, btc_close, alignment_ratio = _contiguous_aligned_closes(
        asset_bars, benchmark_bars, interval_seconds
    )
    # Live caches retain this preferred tail. Historical replay must use the
    # same tail rather than silently gaining a deeper empirical distribution
    # merely because the fixture contains older candles.
    preferred_bars = max(
        config.beta_window,
        config.persistence_window,
        config.long_horizon_bars + config.min_empirical_windows,
    ) + 1
    if len(timestamps) > preferred_bars:
        timestamps = timestamps[-preferred_bars:]
        asset_close = asset_close[-preferred_bars:]
        btc_close = btc_close[-preferred_bars:]
    # A newer listing can be scored after the stated minimum, but its shorter
    # beta sample remains visible through ``beta.observations``. Requiring the
    # entire preferred window here would make ``min_beta_points`` a lying knob.
    required_bars = max(
        config.min_beta_points,
        config.persistence_window,
        config.long_horizon_bars + config.min_empirical_windows,
    ) + 1
    base_flags: list[str] = []
    if alignment_ratio < config.min_alignment_ratio:
        base_flags.append("stale_or_misaligned")
    if timestamps and (
        as_of - (timestamps[-1] + timedelta(seconds=interval_seconds))
        > timedelta(seconds=interval_seconds)
    ):
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
    secondary_factor_returns: Optional[list[float]] = None
    secondary_primary_beta: Optional[float] = None
    if secondary_benchmark and secondary_bars:
        secondary_by_time = {candle.timestamp: candle.close for candle in secondary_bars}
        if all(timestamp in secondary_by_time for timestamp in timestamps):
            secondary_close = [secondary_by_time[timestamp] for timestamp in timestamps]
            secondary_returns = _log_returns(secondary_close)
            secondary_vs_btc = estimate_robust_ewma_factor_model(
                secondary_returns[-config.beta_window:],
                btc_returns[-config.beta_window:],
                config.min_beta_points,
                config.beta_half_life_bars,
                config.winsor_mad,
            )
            if secondary_vs_btc is not None:
                secondary_primary_beta = secondary_vs_btc.beta
                secondary_factor_returns = [
                    secondary_return - secondary_vs_btc.beta * btc_return
                    for secondary_return, btc_return in zip(secondary_returns, btc_returns)
                ]
            else:
                base_flags.append("secondary_factor_unavailable")
        else:
            base_flags.append("secondary_factor_unavailable")

    beta = estimate_robust_ewma_factor_model(
        asset_returns[-config.beta_window:],
        btc_returns[-config.beta_window:],
        config.min_beta_points,
        config.beta_half_life_bars,
        config.winsor_mad,
        (
            secondary_factor_returns[-config.beta_window:]
            if secondary_factor_returns is not None else None
        ),
        secondary_benchmark,
        secondary_primary_beta,
    )
    if beta is None:
        return RelativeStrength(
            symbol=symbol, benchmark=benchmark, as_of=as_of, score=None, beta=None,
            horizon_z={}, persistence=None, acceleration=None, alignment_ratio=alignment_ratio,
            quality_flags=tuple(base_flags), reason="unable to estimate beta from valid benchmark returns",
        )

    residuals = []
    for index, (asset_return, btc_return) in enumerate(zip(asset_returns, btc_returns)):
        factor_return = asset_return - beta.beta * btc_return
        if secondary_factor_returns is not None and beta.secondary_beta is not None:
            factor_return -= beta.secondary_beta * secondary_factor_returns[index]
        residuals.append(factor_return)

    horizons: dict[str, Optional[float]] = {}
    horizon_returns: dict[str, Optional[float]] = {}
    horizon_percentiles: dict[str, Optional[float]] = {}
    for name, bars in (
        ("short", config.short_horizon_bars),
        ("medium", config.medium_horizon_bars),
        ("long", config.long_horizon_bars),
    ):
        z_score, excess_return, percentile = _empirical_horizon(
            residuals, bars, config.min_empirical_windows
        )
        horizons[name] = z_score
        horizon_returns[name] = excess_return
        horizon_percentiles[name] = percentile
    persistence = _persistence(residuals, config.persistence_window)
    acceleration = _acceleration(
        residuals,
        config.short_horizon_bars,
        config.medium_horizon_bars,
        beta.residual_volatility,
    )

    flags = base_flags
    if beta.r_squared < 0.05:
        flags.append("low_beta_fit")
    if (
        beta.beta_standard_error is not None
        and beta.beta_standard_error > max(0.25, abs(beta.beta) * 0.5)
    ):
        flags.append("high_beta_uncertainty")
    if (
        beta.secondary_beta is not None
        and beta.secondary_beta_standard_error is not None
        and beta.secondary_beta_standard_error
        > max(0.25, abs(beta.secondary_beta) * 0.5)
        and "high_beta_uncertainty" not in flags
    ):
        flags.append("high_beta_uncertainty")
    if beta.residual_volatility <= 1e-12:
        flags.append("near_zero_residual_volatility")
    score = _score(horizons, persistence, acceleration)
    discovery_score = _discovery_score(horizons, acceleration)
    reason = None if score is not None else "insufficient usable variation for multi-horizon score"
    return RelativeStrength(
        symbol=symbol, benchmark=benchmark, as_of=as_of, score=score, beta=beta,
        horizon_z=horizons, persistence=persistence, acceleration=acceleration,
        alignment_ratio=alignment_ratio, quality_flags=tuple(flags), reason=reason,
        horizon_excess_return=horizon_returns,
        horizon_percentile=horizon_percentiles,
        model_version="robust_ewma_empirical_discovery_v2",
        discovery_score=discovery_score,
    )
