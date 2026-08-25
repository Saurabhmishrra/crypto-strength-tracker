"""Ranked, explainable scanner built from pure structure and RS features."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence

from .market_structure import MarketStructureConfig, build_market_structure
from .models import Candle, MarketContext, ScanRow, SetupAssessment
from .relative_strength import RSConfig, compute_relative_strength


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
    market: MarketStructureConfig = MarketStructureConfig()
    rs: RSConfig = RSConfig()

    def __post_init__(self) -> None:
        if self.interval_seconds != self.rs.bar_interval_seconds:
            raise ValueError(
                "ScannerConfig interval_seconds must match RSConfig bar_interval_seconds"
            )


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
        )

    if long_regime and live_side == "LONG":
        reasons.extend(("persistent beta-adjusted strength", "current mid above TC and pivot"))
        if _crossed_up(price, prior_price, market.pivots.r1):
            reasons.append("fresh R1 mid-price cross")
        return SetupAssessment(
            "LONG_CANDIDATE", "LONG", strength, tuple(reasons),
            ("awaiting completed-bar confirmation",),
            confirmation="PROVISIONAL", confirmation_price=confirmed_price,
        )
    if short_regime and live_side == "SHORT":
        reasons.extend(("persistent beta-adjusted weakness", "current mid below BC and pivot"))
        if _crossed_down(price, prior_price, market.pivots.s1):
            reasons.append("fresh S1 mid-price cross")
        return SetupAssessment(
            "SHORT_CANDIDATE", "SHORT", strength, tuple(reasons),
            ("awaiting completed-bar confirmation",),
            confirmation="PROVISIONAL", confirmation_price=confirmed_price,
        )

    if rs.score >= config.candidate_rs_score:
        reasons.append("strong RS")
        if rs.persistence < config.candidate_persistence:
            blockers.append("persistence has not cleared the long threshold")
        if live_side != "LONG" and confirmed_side != "LONG":
            blockers.append("neither current mid nor completed close has bullish structure")
        return SetupAssessment("WATCH", "LONG", strength, tuple(reasons), tuple(blockers))
    if rs.score <= -config.candidate_rs_score:
        reasons.append("weak RS")
        if rs.persistence > -config.candidate_persistence:
            blockers.append("persistence has not cleared the short threshold")
        if live_side != "SHORT" and confirmed_side != "SHORT":
            blockers.append("neither current mid nor completed close has bearish structure")
        return SetupAssessment("WATCH", "SHORT", strength, tuple(reasons), tuple(blockers))
    return SetupAssessment("NEUTRAL", "NONE", strength, (), ("no persistent relative-strength regime",))


def scan_assets(
    assets: Mapping[str, AssetInput], as_of: datetime, config: ScannerConfig = ScannerConfig()
) -> list[ScanRow]:
    """Build a single deterministic scan from one coherent candle snapshot."""
    benchmark = assets.get(config.benchmark)
    if benchmark is None:
        raise ValueError(f"benchmark {config.benchmark} is missing")
    secondary = (
        assets.get(config.rs.secondary_benchmark)
        if config.rs.secondary_benchmark else None
    )
    rows: list[ScanRow] = []
    for symbol, asset in sorted(assets.items()):
        if symbol == config.benchmark:
            continue
        market = build_market_structure(
            symbol=symbol, closed_daily=asset.daily, price=asset.price,
            session_open=asset.session_open, as_of=as_of, config=config.market,
        )
        rs = compute_relative_strength(
            symbol=symbol, asset_bars=asset.intraday, benchmark=config.benchmark,
            benchmark_bars=benchmark.intraday, interval_seconds=config.interval_seconds,
            as_of=as_of, config=config.rs,
            secondary_benchmark=(
                config.rs.secondary_benchmark
                if secondary is not None and symbol != config.rs.secondary_benchmark else None
            ),
            secondary_bars=(
                secondary.intraday
                if secondary is not None and symbol != config.rs.secondary_benchmark else ()
            ),
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
