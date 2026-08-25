"""Pure daily CPR, pivot, volatility, and level-context calculations."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from .models import CPRLevels, Candle, MarketStructure, PivotLevels


@dataclass(frozen=True)
class MarketStructureConfig:
    atr_period: int = 14
    realized_vol_period: int = 20
    width_history: int = 120
    minimum_width_history: int = 20


def calculate_cpr(high: float, low: float, close: float) -> CPRLevels:
    """Conventional CPR: P=(H+L+C)/3, BC=(H+L)/2, TC=2P-BC."""
    if (
        not all(math.isfinite(value) for value in (high, low, close))
        or min(high, low, close) <= 0
        or high < low
    ):
        raise ValueError("invalid HLC for CPR")
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = 2.0 * pivot - bc
    return CPRLevels(pivot=pivot, bc=bc, tc=tc)


def calculate_floor_pivots(high: float, low: float, close: float) -> PivotLevels:
    """Conventional floor-trader pivots derived from a completed session."""
    if (
        not all(math.isfinite(value) for value in (high, low, close))
        or min(high, low, close) <= 0
        or high < low
    ):
        raise ValueError("invalid HLC for pivots")
    pivot = (high + low + close) / 3.0
    return PivotLevels(
        pivot=pivot,
        r1=2.0 * pivot - low,
        r2=pivot + (high - low),
        r3=high + 2.0 * (pivot - low),
        s1=2.0 * pivot - high,
        s2=pivot - (high - low),
        s3=low - 2.0 * (high - pivot),
    )


def _validate_daily(candles: Sequence[Candle]) -> list[Candle]:
    ordered = sorted(candles, key=lambda c: c.timestamp)
    if len({c.timestamp for c in ordered}) != len(ordered):
        raise ValueError("daily candles contain duplicate timestamps")
    if len(ordered) < 2:
        raise ValueError("at least two completed daily candles are required")
    return ordered


def _percentile(value: float, history: Sequence[float]) -> Optional[float]:
    if not history:
        return None
    return sum(x <= value for x in history) / len(history)


def _atr(candles: Sequence[Candle], period: int) -> Optional[float]:
    if period <= 0 or len(candles) < period + 1:
        return None
    true_ranges: list[float] = []
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    # Wilder's ATR: seed with a simple period mean, then recursively smooth all
    # later true ranges. This matches the convention traders normally mean by
    # "ATR" and avoids silently presenting a rolling SMA of true range instead.
    atr = statistics.fmean(true_ranges[:period])
    for true_range in true_ranges[period:]:
        atr = ((period - 1) * atr + true_range) / period
    return atr


def _realized_volatility(candles: Sequence[Candle], period: int) -> Optional[float]:
    if period <= 1 or len(candles) < period + 1:
        return None
    returns = [math.log(current.close / previous.close) for previous, current in zip(candles, candles[1:])]
    sample = returns[-period:]
    if len(sample) < 2:
        return None
    return statistics.stdev(sample) * math.sqrt(365.0)


def cpr_position(price: float, cpr: CPRLevels) -> str:
    if price > cpr.top:
        return "above_tc"
    if price < cpr.bottom:
        return "below_bc"
    return "inside_cpr"


def pivot_position(price: float, pivots: PivotLevels) -> str:
    if price > pivots.r3:
        return "above_r3"
    if price > pivots.r2:
        return "r2_to_r3"
    if price > pivots.r1:
        return "r1_to_r2"
    if price > pivots.pivot:
        return "pivot_to_r1"
    if price >= pivots.s1:
        return "s1_to_pivot"
    if price >= pivots.s2:
        return "s2_to_s1"
    if price >= pivots.s3:
        return "s3_to_s2"
    return "below_s3"


def build_market_structure(
    symbol: str,
    closed_daily: Sequence[Candle],
    price: float,
    session_open: Optional[float],
    as_of: datetime,
    config: MarketStructureConfig = MarketStructureConfig(),
) -> MarketStructure:
    """Build active-session context from completed daily candles only.

    The final input candle is *yesterday's completed session*. Its levels apply
    to the current session at ``as_of``. Passing a forming daily candle is an
    input error at the adapter boundary, not something this function guesses at.
    """
    if not math.isfinite(price) or price <= 0:
        raise ValueError("price must be positive")
    if session_open is not None and (
        not math.isfinite(session_open) or session_open <= 0
    ):
        raise ValueError("session_open must be positive")
    daily = _validate_daily(closed_daily)
    prior, active = daily[-2], daily[-1]
    active_cpr = calculate_cpr(active.high, active.low, active.close)
    previous_cpr = calculate_cpr(prior.high, prior.low, prior.close)
    pivots = calculate_floor_pivots(active.high, active.low, active.close)

    widths = [calculate_cpr(c.high, c.low, c.close).normalized_width for c in daily]
    history = widths[max(0, len(widths) - 1 - config.width_history):-1]
    width_pct = _percentile(active_cpr.normalized_width, history) if len(history) >= config.minimum_width_history else None
    if width_pct is None:
        regime = "unknown"
    elif width_pct <= 0.25:
        regime = "tight"
    elif width_pct >= 0.75:
        regime = "wide"
    else:
        regime = "normal"

    atr = _atr(daily, config.atr_period)
    distances = {
        name: ((price - level) / atr if atr and atr > 0 else None)
        for name, level in (("BC", active_cpr.bottom), ("P", active_cpr.pivot), ("TC", active_cpr.top), *pivots.ordered())
    }
    return MarketStructure(
        symbol=symbol,
        as_of=as_of,
        price=price,
        session_open=session_open,
        active_cpr=active_cpr,
        previous_cpr=previous_cpr,
        pivots=pivots,
        cpr_width_percentile=width_pct,
        cpr_regime=regime,
        atr=atr,
        realized_volatility=_realized_volatility(daily, config.realized_vol_period),
        price_cpr_position=cpr_position(price, active_cpr),
        opening_cpr_position=cpr_position(session_open, active_cpr) if session_open is not None else None,
        pivot_position=pivot_position(price, pivots),
        level_distances_atr=distances,
    )
