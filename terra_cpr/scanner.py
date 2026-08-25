"""Ranked, explainable scanner built from pure structure and RS features."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence

from .market_structure import MarketStructureConfig, build_market_structure
from .models import Candle, MarketContext, ScanRow, SetupAssessment
from .relative_strength import RSConfig, compute_relative_strength


BROAD_ALT_FACTOR = "BROAD_ALT_L1O"


@dataclass(frozen=True)
class AssetInput:
    symbol: str
    price: float
    daily: Sequence[Candle]
    intraday: Sequence[Candle]
    session_open: Optional[float] = None
    prior_price: Optional[float] = None
    context: Optional[MarketContext] = None


@dataclass(frozen=True)
class ScannerConfig:
    benchmark: str = "BTC"
    interval_seconds: int = 3600
    candidate_rs_score: float = 3.5
    candidate_persistence: float = 0.40
    early_discovery_score: float = 2.5
    strong_discovery_score: float = 3.0
    market: MarketStructureConfig = MarketStructureConfig()
    rs: RSConfig = RSConfig()

    def __post_init__(self) -> None:
        if self.interval_seconds != self.rs.bar_interval_seconds:
            raise ValueError(
                "ScannerConfig interval_seconds must match RSConfig bar_interval_seconds"
            )
        if not (
            0.0 < self.early_discovery_score
            <= self.strong_discovery_score
            <= self.candidate_rs_score
        ):
            raise ValueError(
                "discovery thresholds must satisfy 0 < early <= strong <= candidate"
            )
        if not 0.0 <= self.candidate_persistence <= 1.0:
            raise ValueError("candidate_persistence must be in [0, 1]")


def build_broad_alt_factors(
    assets: Mapping[str, AssetInput],
    benchmark: str,
    interval_seconds: int,
    min_constituents: int,
) -> dict[str, tuple[list[Candle], int]]:
    """Build equal-weight, leave-one-out broad-alt factors from known bars.

    Each target gets a synthetic factor that excludes both BTC and itself. The
    universe is the scanner's point-in-time selected panel, so no outcome-period
    membership is introduced. A missing or undersized cross-section creates a
    gap and makes the secondary factor unavailable rather than silently filling
    it with zero.
    """
    benchmark_asset = assets.get(benchmark)
    if benchmark_asset is None:
        return {}
    targets = sorted(symbol for symbol in assets if symbol != benchmark)
    if not targets:
        return {}
    closes = {
        symbol: {bar.timestamp: bar.close for bar in asset.intraday}
        for symbol, asset in assets.items() if symbol != benchmark
    }
    timestamps = sorted({bar.timestamp for bar in benchmark_asset.intraday})
    if not timestamps:
        return {}
    step = timedelta(seconds=interval_seconds)
    prices = {symbol: 100.0 for symbol in targets}
    bars: dict[str, list[Candle]] = {symbol: [] for symbol in targets}
    minimum_seen = {symbol: math.inf for symbol in targets}

    for timestamp in timestamps[1:]:
        previous = timestamp - step
        returns: dict[str, float] = {}
        for symbol, by_time in closes.items():
            prior_close = by_time.get(previous)
            current_close = by_time.get(timestamp)
            if prior_close is None or current_close is None:
                continue
            returns[symbol] = math.log(current_close / prior_close)
        total = sum(returns.values())
        count = len(returns)
        for target in targets:
            target_return = returns.get(target)
            constituent_count = count - (1 if target_return is not None else 0)
            if constituent_count < min_constituents:
                # A factor return cannot be inferred from an undersized panel.
                # Discard the earlier segment so a later valid observation does
                # not bridge the gap with a synthetic zero return.
                prices[target] = 100.0
                bars[target] = []
                minimum_seen[target] = math.inf
                continue
            if not bars[target]:
                bars[target].append(Candle(
                    previous, 100.0, 100.0, 100.0, 100.0,
                    float(constituent_count),
                ))
            factor_return = (
                total - (target_return if target_return is not None else 0.0)
            ) / constituent_count
            prices[target] *= math.exp(factor_return)
            price = prices[target]
            bars[target].append(Candle(
                timestamp, price, price, price, price, float(constituent_count)
            ))
            minimum_seen[target] = min(minimum_seen[target], constituent_count)

    return {
        symbol: (factor_bars, int(minimum_seen[symbol]))
        for symbol, factor_bars in bars.items()
        if minimum_seen[symbol] != math.inf
    }


def _crossed_up(price: float, prior: Optional[float], level: float) -> bool:
    return prior is not None and prior <= level < price


def _crossed_down(price: float, prior: Optional[float], level: float) -> bool:
    return prior is not None and prior >= level > price


def _row_order(row: ScanRow) -> tuple[bool, float, str]:
    score = float(row.rs.score) if row.rs.score is not None else float("-inf")
    return row.strong_rank is None, -score, row.symbol


def _structure_side(price: float, market) -> str:
    if price > market.active_cpr.top and price > market.pivots.pivot:
        return "LONG"
    if price < market.active_cpr.bottom and price < market.pivots.pivot:
        return "SHORT"
    return "NONE"


def assess_setup(
    row_symbol: str,
    price: float,
    prior_price: Optional[float],
    market,
    rs,
    config: ScannerConfig,
    confirmed_price: Optional[float] = None,
    confirmed_prior_price: Optional[float] = None,
) -> SetupAssessment:
    """Produce a research label, never an execution instruction."""
    if not rs.is_usable or rs.score is None or rs.persistence is None:
        return SetupAssessment(
            label="INSUFFICIENT_DATA", direction="NONE", strength=0.0,
            reasons=(), blockers=tuple(rs.quality_flags) + ((rs.reason,) if rs.reason else ()),
        )

    live_side = _structure_side(price, market)
    confirmed_side = _structure_side(confirmed_price, market) if confirmed_price is not None else "NONE"
    long_regime = rs.score >= config.candidate_rs_score and rs.persistence >= config.candidate_persistence
    short_regime = rs.score <= -config.candidate_rs_score and rs.persistence <= -config.candidate_persistence
    reasons: list[str] = []
    blockers: list[str] = []
    discovery_score = getattr(rs, "discovery_score", None)
    if discovery_score is None:
        # Compatibility for callers that supply a lightweight RS test double.
        discovery_score = rs.score
    strength = min(100.0, abs(rs.score) * 10.0)

    if long_regime and confirmed_side == "LONG":
        reasons.extend((
            "persistent beta-adjusted strength",
            "completed bar closed above TC",
            "completed bar closed above daily pivot",
        ))
        if _crossed_up(confirmed_price, confirmed_prior_price, market.pivots.r1):
            reasons.append("fresh completed close above R1")
            strength = min(100.0, strength + 10.0)
        if live_side != "LONG":
            blockers.append("current mid has moved back inside confirmed bullish structure")
        return SetupAssessment(
            "LONG_CANDIDATE", "LONG", strength, tuple(reasons), tuple(blockers),
            confirmation="CONFIRMED", confirmation_price=confirmed_price,
            discovery_tier="CANDIDATE_GRADE",
        )
    if short_regime and confirmed_side == "SHORT":
        reasons.extend((
            "persistent beta-adjusted weakness",
            "completed bar closed below BC",
            "completed bar closed below daily pivot",
        ))
        if _crossed_down(confirmed_price, confirmed_prior_price, market.pivots.s1):
            reasons.append("fresh completed close below S1")
            strength = min(100.0, strength + 10.0)
        if live_side != "SHORT":
            blockers.append("current mid has moved back inside confirmed bearish structure")
        return SetupAssessment(
            "SHORT_CANDIDATE", "SHORT", strength, tuple(reasons), tuple(blockers),
            confirmation="CONFIRMED", confirmation_price=confirmed_price,
            discovery_tier="CANDIDATE_GRADE",
        )

    if long_regime and live_side == "LONG":
        reasons.extend(("persistent beta-adjusted strength", "current mid above TC and pivot"))
        if _crossed_up(price, prior_price, market.pivots.r1):
            reasons.append("fresh R1 mid-price cross")
        return SetupAssessment(
            "LONG_CANDIDATE", "LONG", strength, tuple(reasons),
            ("awaiting completed-bar confirmation",),
            confirmation="PROVISIONAL", confirmation_price=confirmed_price,
            discovery_tier="CANDIDATE_GRADE",
        )
    if short_regime and live_side == "SHORT":
        reasons.extend(("persistent beta-adjusted weakness", "current mid below BC and pivot"))
        if _crossed_down(price, prior_price, market.pivots.s1):
            reasons.append("fresh S1 mid-price cross")
        return SetupAssessment(
            "SHORT_CANDIDATE", "SHORT", strength, tuple(reasons),
            ("awaiting completed-bar confirmation",),
            confirmation="PROVISIONAL", confirmation_price=confirmed_price,
            discovery_tier="CANDIDATE_GRADE",
        )

    if rs.score >= config.candidate_rs_score:
        reasons.append("strong RS")
        if rs.persistence < config.candidate_persistence:
            blockers.append("persistence has not cleared the long threshold")
        if live_side != "LONG" and confirmed_side != "LONG":
            blockers.append("neither current mid nor completed close has bullish structure")
        return SetupAssessment(
            "WATCH", "LONG", strength, tuple(reasons), tuple(blockers),
            discovery_tier="CANDIDATE_GRADE",
        )
    if rs.score <= -config.candidate_rs_score:
        reasons.append("weak RS")
        if rs.persistence > -config.candidate_persistence:
            blockers.append("persistence has not cleared the short threshold")
        if live_side != "SHORT" and confirmed_side != "SHORT":
            blockers.append("neither current mid nor completed close has bearish structure")
        return SetupAssessment(
            "WATCH", "SHORT", strength, tuple(reasons), tuple(blockers),
            discovery_tier="CANDIDATE_GRADE",
        )

    discovery_strength = min(100.0, abs(discovery_score) * 10.0)
    if abs(discovery_score) >= config.strong_discovery_score:
        direction = "LONG" if discovery_score > 0 else "SHORT"
        return SetupAssessment(
            "WATCH", direction, discovery_strength,
            ("strong persistence-free RS discovery",),
            (f"confirmed candidate score has not cleared {config.candidate_rs_score:g}",),
            discovery_tier="STRONG_DISCOVERY",
        )
    if abs(discovery_score) >= config.early_discovery_score:
        direction = "LONG" if discovery_score > 0 else "SHORT"
        return SetupAssessment(
            "WATCH", direction, discovery_strength,
            ("early persistence-free RS discovery",),
            (f"confirmed candidate score has not cleared {config.candidate_rs_score:g}",),
            discovery_tier="EARLY_DISCOVERY",
        )
    return SetupAssessment(
        "NEUTRAL", "NONE", discovery_strength, (),
        ("discovery score is below the early threshold",),
    )


def scan_assets(
    assets: Mapping[str, AssetInput], as_of: datetime, config: ScannerConfig = ScannerConfig()
) -> list[ScanRow]:
    """Build a single deterministic scan from one coherent candle snapshot."""
    benchmark = assets.get(config.benchmark)
    if benchmark is None:
        raise ValueError(f"benchmark {config.benchmark} is missing")
    secondary = (
        assets.get(config.rs.secondary_benchmark)
        if config.rs.secondary_benchmark
        and config.rs.secondary_benchmark != BROAD_ALT_FACTOR else None
    )
    broad_alt_factors = (
        build_broad_alt_factors(
            assets, config.benchmark, config.interval_seconds,
            config.rs.broad_alt_min_constituents,
        )
        if config.rs.secondary_benchmark == BROAD_ALT_FACTOR else {}
    )
    rows: list[ScanRow] = []
    for symbol, asset in sorted(assets.items()):
        if symbol == config.benchmark:
            continue
        market = build_market_structure(
            symbol=symbol, closed_daily=asset.daily, price=asset.price,
            session_open=asset.session_open, as_of=as_of, config=config.market,
        )
        if config.rs.secondary_benchmark == BROAD_ALT_FACTOR:
            factor_bars, factor_constituents = broad_alt_factors.get(
                symbol, ([], None)
            )
            factor_name = BROAD_ALT_FACTOR
        else:
            factor_bars = (
                secondary.intraday
                if secondary is not None and symbol != config.rs.secondary_benchmark else ()
            )
            factor_constituents = (
                1 if secondary is not None and symbol != config.rs.secondary_benchmark else None
            )
            factor_name = (
                config.rs.secondary_benchmark
                if secondary is not None and symbol != config.rs.secondary_benchmark else None
            )
        rs = compute_relative_strength(
            symbol=symbol, asset_bars=asset.intraday, benchmark=config.benchmark,
            benchmark_bars=benchmark.intraday, interval_seconds=config.interval_seconds,
            as_of=as_of, config=config.rs,
            secondary_benchmark=factor_name,
            secondary_bars=factor_bars,
            secondary_factor_constituents=factor_constituents,
        )
        confirmed_price = asset.intraday[-1].close if asset.intraday else None
        confirmed_prior = asset.intraday[-2].close if len(asset.intraday) >= 2 else None
        setup = assess_setup(
            symbol, asset.price, asset.prior_price, market, rs, config,
            confirmed_price=confirmed_price, confirmed_prior_price=confirmed_prior,
        )
        rows.append(ScanRow(
            symbol=symbol, price=asset.price, market=market, rs=rs,
            setup=setup, context=asset.context,
        ))

    # Keep stale observations visible, but never let them set or move a live
    # cross-sectional rank. A stale score is useful diagnostic context, not a
    # comparable observation.
    usable = sorted((r for r in rows if r.rs.is_usable), key=lambda r: (-float(r.rs.score), r.symbol))
    strength_rank = {row.symbol: index for index, row in enumerate(usable, start=1)}
    weakness_rank = {row.symbol: index for index, row in enumerate(reversed(usable), start=1)}
    return [
        ScanRow(
            symbol=row.symbol, price=row.price, market=row.market, rs=row.rs, setup=row.setup,
            strong_rank=strength_rank.get(row.symbol), weak_rank=weakness_rank.get(row.symbol),
            context=row.context,
        )
        for row in sorted(rows, key=_row_order)
    ]
