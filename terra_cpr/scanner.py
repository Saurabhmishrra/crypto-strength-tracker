"""Ranked, explainable scanner built from pure structure and RS features."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence

from .market_structure import MarketStructureConfig, build_market_structure
from .models import Candle, ScanRow, SetupAssessment
from .relative_strength import RSConfig, compute_relative_strength


@dataclass(frozen=True)
class AssetInput:
    symbol: str
    price: float
    daily: Sequence[Candle]
    intraday: Sequence[Candle]
    session_open: Optional[float] = None
    prior_price: Optional[float] = None


@dataclass(frozen=True)
class ScannerConfig:
    benchmark: str = "BTC"
    interval_seconds: int = 3600
    candidate_rs_score: float = 3.5
    candidate_persistence: float = 0.40
    market: MarketStructureConfig = MarketStructureConfig()
    rs: RSConfig = RSConfig()


def _crossed_up(price: float, prior: Optional[float], level: float) -> bool:
    return prior is not None and prior <= level < price


def _crossed_down(price: float, prior: Optional[float], level: float) -> bool:
    return prior is not None and prior >= level > price


def assess_setup(row_symbol: str, price: float, prior_price: Optional[float], market, rs, config: ScannerConfig) -> SetupAssessment:
    """Produce a research label, never an execution instruction."""
    if not rs.is_usable or rs.score is None or rs.persistence is None:
        return SetupAssessment(
            label="INSUFFICIENT_DATA", direction="NONE", strength=0.0,
            reasons=(), blockers=tuple(rs.quality_flags) + ((rs.reason,) if rs.reason else ()),
        )

    bullish_structure = market.price_cpr_position == "above_tc" and price > market.pivots.pivot
    bearish_structure = market.price_cpr_position == "below_bc" and price < market.pivots.pivot
    long_ready = rs.score >= config.candidate_rs_score and rs.persistence >= config.candidate_persistence and bullish_structure
    short_ready = rs.score <= -config.candidate_rs_score and rs.persistence <= -config.candidate_persistence and bearish_structure
    reasons: list[str] = []
    blockers: list[str] = []
    strength = min(100.0, abs(rs.score) * 10.0)

    if long_ready:
        reasons.extend(("persistent beta-adjusted strength", "price accepted above TC", "price above daily pivot"))
        if _crossed_up(price, prior_price, market.pivots.r1):
            reasons.append("fresh R1 acceptance")
            strength = min(100.0, strength + 10.0)
        return SetupAssessment("LONG_CANDIDATE", "LONG", strength, tuple(reasons), ())
    if short_ready:
        reasons.extend(("persistent beta-adjusted weakness", "price accepted below BC", "price below daily pivot"))
        if _crossed_down(price, prior_price, market.pivots.s1):
            reasons.append("fresh S1 acceptance")
            strength = min(100.0, strength + 10.0)
        return SetupAssessment("SHORT_CANDIDATE", "SHORT", strength, tuple(reasons), ())

    if rs.score >= config.candidate_rs_score:
        reasons.append("strong RS")
        if not bullish_structure:
            blockers.append("price has not accepted bullish CPR/pivot structure")
        return SetupAssessment("WATCH", "LONG", strength, tuple(reasons), tuple(blockers))
    if rs.score <= -config.candidate_rs_score:
        reasons.append("weak RS")
        if not bearish_structure:
            blockers.append("price has not accepted bearish CPR/pivot structure")
        return SetupAssessment("WATCH", "SHORT", strength, tuple(reasons), tuple(blockers))
    return SetupAssessment("NEUTRAL", "NONE", strength, (), ("no persistent relative-strength regime",))


def scan_assets(
    assets: Mapping[str, AssetInput], as_of: datetime, config: ScannerConfig = ScannerConfig()
) -> list[ScanRow]:
    """Build a single deterministic scan from one coherent candle snapshot."""
    benchmark = assets.get(config.benchmark)
    if benchmark is None:
        raise ValueError(f"benchmark {config.benchmark} is missing")
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
        )
        setup = assess_setup(symbol, asset.price, asset.prior_price, market, rs, config)
        rows.append(ScanRow(symbol=symbol, price=asset.price, market=market, rs=rs, setup=setup))

    usable = sorted((r for r in rows if r.rs.score is not None), key=lambda r: (-float(r.rs.score), r.symbol))
    strength_rank = {row.symbol: index for index, row in enumerate(usable, start=1)}
    weakness_rank = {row.symbol: index for index, row in enumerate(reversed(usable), start=1)}
    return [
        ScanRow(
            symbol=row.symbol, price=row.price, market=row.market, rs=row.rs, setup=row.setup,
            strong_rank=strength_rank.get(row.symbol), weak_rank=weakness_rank.get(row.symbol),
        )
        for row in sorted(rows, key=lambda r: (r.strong_rank is None, -(r.rs.score or -999.0), r.symbol))
    ]
