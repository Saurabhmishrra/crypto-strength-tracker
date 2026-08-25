"""Transparent event-study metrics for frozen scanner hypotheses."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping, Optional, Sequence

from .models import Candle, MarketContext
from .scanner import (
    BROAD_ALT_FACTOR,
    AssetInput,
    ScannerConfig,
    scan_assets,
)


CURRENT_FACTOR_MODEL = "btc_eth"
BROAD_ALT_FACTOR_MODEL = "btc_broad_alt"


@dataclass(frozen=True)
class ResearchSpecification:
    spec_id: str
    label: str
    event_rule: str
    factor_model: str


FIVE_MODEL_COMPARISON = (
    ResearchSpecification(
        "H1", "Frozen H1", "confirmed_candidate", CURRENT_FACTOR_MODEL
    ),
    ResearchSpecification(
        "D1", "Persistence-free discovery", "strong_discovery",
        CURRENT_FACTOR_MODEL,
    ),
    ResearchSpecification(
        "H5", "Discovery plus completed structure", "h5_discovery_structure",
        CURRENT_FACTOR_MODEL,
    ),
    ResearchSpecification(
        "B1", "24h residual-momentum baseline", "residual_momentum_baseline",
        CURRENT_FACTOR_MODEL,
    ),
    ResearchSpecification(
        "H6", "H5 with BTC plus broad-alt factor", "h5_discovery_structure",
        BROAD_ALT_FACTOR_MODEL,
    ),
)


@dataclass(frozen=True)
class EventOutcome:
    timestamp: datetime
    symbol: str
    direction: str  # LONG or SHORT
    gross_forward_return: float  # decimal asset return over a predetermined horizon
    funding_cost: float = 0.0
    score: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    horizon_bars: int | None = None
    benchmark_forward_return: float | None = None
    btc_beta_adjusted_forward_log_return: float | None = None
    model_factor_adjusted_forward_log_return: float | None = None
    features: Mapping[str, float | None] = field(default_factory=dict)
    event_rule: str = "confirmed_candidate"
    factor_model: str = CURRENT_FACTOR_MODEL

    @property
    def signed_gross_return(self) -> float:
        if self.direction == "LONG":
            return self.gross_forward_return
        if self.direction == "SHORT":
            return -self.gross_forward_return
        raise ValueError(f"unknown direction {self.direction}")

    @property
    def signed_btc_beta_adjusted_return(self) -> float:
        value = self.btc_beta_adjusted_forward_log_return
        if value is None:
            raise ValueError("event has no BTC-beta-adjusted forward outcome")
        simple_return = math.expm1(value)
        if self.direction == "LONG":
            return simple_return
        if self.direction == "SHORT":
            return -simple_return
        raise ValueError(f"unknown direction {self.direction}")

    @property
    def signed_model_factor_adjusted_return(self) -> float:
        value = self.model_factor_adjusted_forward_log_return
        if value is None:
            raise ValueError("event has no model-factor-adjusted forward outcome")
        simple_return = math.expm1(value)
        if self.direction == "LONG":
            return simple_return
        if self.direction == "SHORT":
            return -simple_return
        raise ValueError(f"unknown direction {self.direction}")


@dataclass(frozen=True)
class ResearchMetrics:
    count: int
    win_rate: float | None
    average_win: float | None
    average_loss: float | None
    expectancy: float | None
    profit_factor: float | None
    sharpe: float | None
    max_drawdown: float | None
    total_return: float | None


ContextAt = Callable[[str, datetime], Optional[MarketContext]]
FundingCost = Callable[[str, datetime, datetime, str], float]


def _completed_structure_side(row) -> str:
    """Direction accepted by the completed close used for this replay row."""
    price = row.price
    if price > row.market.active_cpr.top and price > row.market.pivots.pivot:
        return "LONG"
    if price < row.market.active_cpr.bottom and price < row.market.pivots.pivot:
        return "SHORT"
    return "NONE"


def _config_for_factor_model(
    scanner_config: ScannerConfig, factor_model: str
) -> ScannerConfig:
    if factor_model == CURRENT_FACTOR_MODEL:
        return replace(
            scanner_config,
            rs=replace(scanner_config.rs, secondary_benchmark="ETH"),
        )
    if factor_model == BROAD_ALT_FACTOR_MODEL:
        return replace(
            scanner_config,
            rs=replace(scanner_config.rs, secondary_benchmark=BROAD_ALT_FACTOR),
        )
    raise ValueError(f"unknown factor_model {factor_model}")


def _residual_momentum_percentiles(rows) -> dict[str, float]:
    """Average-rank percentiles of completed 24h model residual returns."""
    values = sorted(
        (
            float(row.rs.horizon_excess_return["medium"]),
            row.symbol,
        )
        for row in rows
        if row.rs.is_usable
        and row.rs.horizon_excess_return.get("medium") is not None
    )
    if len(values) < 2:
        return {}
    output: dict[str, float] = {}
    index = 0
    while index < len(values):
        stop = index + 1
        while stop < len(values) and values[stop][0] == values[index][0]:
            stop += 1
        average_rank = (index + stop - 1) / 2.0
        percentile = average_rank / (len(values) - 1)
        for _value, symbol in values[index:stop]:
            output[symbol] = percentile
        index = stop
    return output


def _broad_alt_forward_log_return(
    closes_by_symbol: Mapping[str, Mapping[datetime, float]],
    constituents: Sequence[str],
    target: str,
    entry_open: datetime,
    exit_open: datetime,
    step_seconds: int,
    min_constituents: int,
) -> Optional[float]:
    """Forward return of the entry-time leave-one-out equal-weight alt factor."""
    total = 0.0
    timestamp = entry_open + timedelta(seconds=step_seconds)
    step = timedelta(seconds=step_seconds)
    while timestamp <= exit_open:
        previous = timestamp - step
        returns = []
        for symbol in constituents:
            if symbol == target:
                continue
            by_time = closes_by_symbol.get(symbol, {})
            prior_close = by_time.get(previous)
            current_close = by_time.get(timestamp)
            if prior_close is not None and current_close is not None:
                returns.append(math.log(current_close / prior_close))
        if len(returns) < min_constituents:
            return None
        total += statistics.fmean(returns)
        timestamp += step
    return total


def _bars_closed_by(
    candles: Sequence[Candle], as_of: datetime, interval_seconds: int
) -> list[Candle]:
    return [
        candle for candle in candles
        if candle.timestamp + timedelta(seconds=interval_seconds) <= as_of
    ]


def _historical_volume_context(
    bars: Sequence[Candle], interval_seconds: int
) -> MarketContext:
    bars_per_day = max(1, math.ceil(86_400 / interval_seconds))
    current = bars[-bars_per_day:]
    current_notional = sum(candle.close * candle.volume for candle in current)
    prior_blocks = []
    end = max(0, len(bars) - bars_per_day)
    for stop in range(end, max(0, end - 20 * bars_per_day), -bars_per_day):
        block = bars[max(0, stop - bars_per_day):stop]
        if len(block) == bars_per_day:
            prior_blocks.append(sum(candle.close * candle.volume for candle in block))
    baseline = statistics.median(prior_blocks) if prior_blocks else None
    return MarketContext(
        source="point_in_time_candles",
        day_notional_volume=current_notional,
        relative_notional_volume=(
            current_notional / baseline if baseline and baseline > 0 else None
        ),
    )


def _select_point_in_time_universe(
    panel: Mapping[str, AssetInput],
    size: Optional[int],
    required: Sequence[str],
) -> dict[str, AssetInput]:
    if size is None or len(panel) <= size:
        return dict(panel)
    if size < len(set(required)):
        raise ValueError("universe_size is smaller than the required factor set")
    ranked = sorted(
        panel.values(),
        key=lambda asset: (
            -float(asset.context.day_notional_volume or 0.0)
            if asset.context else 0.0,
            asset.symbol,
        ),
    )
    selected = {asset.symbol: asset for asset in ranked[:size]}
    required_set = set(required)
    for symbol in required:
        if symbol not in panel or symbol in selected:
            continue
        replace_symbol = next(
            candidate.symbol for candidate in reversed(ranked)
            if candidate.symbol in selected and candidate.symbol not in required_set
        )
        del selected[replace_symbol]
        selected[symbol] = panel[symbol]
    return selected


def _generate_point_in_time_events_for_rules(
    assets: Mapping[str, AssetInput],
    scanner_config: ScannerConfig,
    horizon_bars: int,
    universe_size: Optional[int] = None,
    context_at: Optional[ContextAt] = None,
    funding_cost: Optional[FundingCost] = None,
    event_rules: Sequence[str] = ("confirmed_candidate",),
    factor_model: str = CURRENT_FACTOR_MODEL,
) -> list[EventOutcome]:
    """Generate completed-close rule activations without future leakage.

    Universe membership, feature history, beta/RS inputs, and daily structure are
    rebuilt at every timestamp. ``confirmed_candidate`` preserves frozen H1.
    The discovery rules trigger from completed-bar persistence-free scores only.
    H5 additionally requires matching completed-close CPR and pivot structure.
    They are research events, not scanner alerts. Outcomes are attached only
    after the complete event set has been generated.
    """
    if horizon_bars <= 0:
        raise ValueError("horizon_bars must be positive")
    allowed_rules = {
        "confirmed_candidate", "early_discovery", "strong_discovery",
        "h5_discovery_structure", "residual_momentum_baseline",
    }
    if not event_rules:
        raise ValueError("event_rules must not be empty")
    unknown_rules = sorted(set(event_rules).difference(allowed_rules))
    if unknown_rules:
        raise ValueError(f"unknown event_rule {unknown_rules[0]}")
    effective_config = _config_for_factor_model(scanner_config, factor_model)
    benchmark = assets.get(effective_config.benchmark)
    if benchmark is None:
        raise ValueError(f"benchmark {effective_config.benchmark} is missing")
    step = effective_config.interval_seconds
    benchmark_bars = sorted(benchmark.intraday, key=lambda candle: candle.timestamp)
    closes_by_symbol = {
        symbol: {candle.timestamp: candle.close for candle in asset.intraday}
        for symbol, asset in assets.items()
    }
    pending: list[tuple[
        str, datetime, str, str, float, float,
        Mapping[str, float | None], tuple[str, ...]
    ]] = []
    prior_states: dict[str, dict[str, tuple[str, str]]] = {
        event_rule: {} for event_rule in event_rules
    }

    for benchmark_index in range(0, max(0, len(benchmark_bars) - horizon_bars)):
        bar = benchmark_bars[benchmark_index]
        as_of = bar.timestamp + timedelta(seconds=step)
        panel: dict[str, AssetInput] = {}
        for symbol, source in assets.items():
            intraday = _bars_closed_by(source.intraday, as_of, step)
            if not intraday or intraday[-1].timestamp != bar.timestamp:
                continue
            daily = _bars_closed_by(source.daily, as_of, 86_400)
            if len(daily) < 2:
                continue
            context = (
                context_at(symbol, as_of) if context_at else None
            ) or _historical_volume_context(intraday, step)
            panel[symbol] = AssetInput(
                symbol=symbol,
                price=intraday[-1].close,
                daily=daily,
                intraday=intraday,
                prior_price=intraday[-2].close if len(intraday) >= 2 else None,
                context=context,
            )
        required = [effective_config.benchmark]
        secondary = effective_config.rs.secondary_benchmark
        if secondary and secondary != BROAD_ALT_FACTOR and secondary in panel:
            required.append(secondary)
        if effective_config.benchmark not in panel:
            continue
        selected = _select_point_in_time_universe(panel, universe_size, required)
        rows = scan_assets(selected, as_of, effective_config)
        usable_scores = [float(row.rs.score) for row in rows if row.rs.is_usable]
        positive_breadth = (
            sum(score > 0 for score in usable_scores) / len(usable_scores)
            if usable_scores else None
        )
        momentum_percentiles = _residual_momentum_percentiles(rows)
        factor_constituents = tuple(sorted(
            symbol for symbol in selected
            if symbol != effective_config.benchmark
        ))
        for event_rule in event_rules:
            current_states: dict[str, tuple[str, str]] = {}
            for row in rows:
                if (
                    factor_model == BROAD_ALT_FACTOR_MODEL
                    and (
                        row.rs.beta is None
                        or row.rs.beta.secondary_benchmark != BROAD_ALT_FACTOR
                        or row.rs.beta.secondary_beta is None
                    )
                ):
                    continue
                if event_rule == "confirmed_candidate":
                    if (
                        not row.setup.label.endswith("CANDIDATE")
                        or row.setup.confirmation != "CONFIRMED"
                    ):
                        continue
                    direction = row.setup.direction
                    trigger_score = row.rs.score
                    state = (row.setup.label, direction)
                elif event_rule == "residual_momentum_baseline":
                    percentile = momentum_percentiles.get(row.symbol)
                    residual_return = row.rs.horizon_excess_return.get("medium")
                    trigger_score = row.rs.horizon_z.get("medium")
                    if (
                        percentile is None or residual_return is None
                        or trigger_score is None
                    ):
                        continue
                    if percentile >= 0.80 and residual_return > 0:
                        direction = "LONG"
                    elif percentile <= 0.20 and residual_return < 0:
                        direction = "SHORT"
                    else:
                        continue
                    state = (event_rule, direction)
                else:
                    trigger_score = row.rs.discovery_score
                    threshold = (
                        scanner_config.early_discovery_score
                        if event_rule == "early_discovery"
                        else scanner_config.strong_discovery_score
                    )
                    if trigger_score is None or abs(trigger_score) < threshold:
                        continue
                    direction = "LONG" if trigger_score > 0 else "SHORT"
                    if (
                        event_rule == "h5_discovery_structure"
                        and _completed_structure_side(row) != direction
                    ):
                        continue
                    state = (event_rule, direction)
                current_states[row.symbol] = state
                if prior_states[event_rule].get(row.symbol) == state:
                    continue
                context = row.context
                features: dict[str, float | None] = {
                    "rs_score": row.rs.score,
                    "discovery_score": row.rs.discovery_score,
                    "persistence": row.rs.persistence,
                    "acceleration": row.rs.acceleration,
                    "beta": row.rs.beta.beta if row.rs.beta else None,
                    "beta_standard_error": (
                        row.rs.beta.beta_standard_error if row.rs.beta else None
                    ),
                    "secondary_beta": (
                        row.rs.beta.secondary_beta if row.rs.beta else None
                    ),
                    "secondary_primary_beta": (
                        row.rs.beta.secondary_primary_beta if row.rs.beta else None
                    ),
                    "beta_r_squared": row.rs.beta.r_squared if row.rs.beta else None,
                    "cpr_width_percentile": row.market.cpr_width_percentile,
                    "relative_notional_volume": (
                        context.relative_notional_volume if context else None
                    ),
                    "day_notional_volume": (
                        context.day_notional_volume if context else None
                    ),
                    "impact_spread_bps": (
                        context.impact_spread_bps if context else None
                    ),
                    "funding_rate": context.funding_rate if context else None,
                    "open_interest": context.open_interest if context else None,
                    "positive_rs_breadth": positive_breadth,
                    "residual_momentum_24h": (
                        row.rs.horizon_excess_return.get("medium")
                    ),
                    "residual_momentum_percentile": (
                        momentum_percentiles.get(row.symbol)
                    ),
                    "secondary_factor_constituents": (
                        row.rs.secondary_factor_constituents
                    ),
                    "completed_structure": (
                        1.0 if _completed_structure_side(row) == direction else 0.0
                    ),
                    "trigger_threshold": (
                        scanner_config.candidate_rs_score
                        if event_rule == "confirmed_candidate"
                        else 0.80
                        if event_rule == "residual_momentum_baseline"
                        else scanner_config.early_discovery_score
                        if event_rule == "early_discovery"
                        else scanner_config.strong_discovery_score
                    ),
                }
                pending.append((
                    event_rule, as_of, row.symbol, direction, row.price,
                    float(trigger_score), features, factor_constituents,
                ))
            prior_states[event_rule] = current_states

    events: list[EventOutcome] = []
    for (
        event_rule, timestamp, symbol, direction, entry_price, score, features,
        factor_constituents,
    ) in pending:
        entry_open = timestamp - timedelta(seconds=step)
        exit_open = entry_open + timedelta(seconds=step * horizon_bars)
        exit_price = closes_by_symbol.get(symbol, {}).get(exit_open)
        benchmark_entry = closes_by_symbol.get(effective_config.benchmark, {}).get(entry_open)
        benchmark_exit = closes_by_symbol.get(effective_config.benchmark, {}).get(exit_open)
        if exit_price is None or benchmark_entry is None or benchmark_exit is None:
            continue
        asset_forward_return = exit_price / entry_price - 1.0
        benchmark_forward_return = benchmark_exit / benchmark_entry - 1.0
        beta = features.get("beta")
        beta_adjusted = (
            math.log(exit_price / entry_price)
            - float(beta) * math.log(benchmark_exit / benchmark_entry)
            if beta is not None else None
        )
        model_adjusted = beta_adjusted
        secondary_symbol = effective_config.rs.secondary_benchmark
        secondary_beta = features.get("secondary_beta")
        secondary_primary_beta = features.get("secondary_primary_beta")
        if (
            beta_adjusted is not None
            and secondary_symbol == BROAD_ALT_FACTOR
            and secondary_beta is not None
            and secondary_primary_beta is not None
        ):
            broad_forward = _broad_alt_forward_log_return(
                closes_by_symbol, factor_constituents, symbol,
                entry_open, exit_open, step,
                effective_config.rs.broad_alt_min_constituents,
            )
            if broad_forward is None:
                model_adjusted = None
            else:
                secondary_factor_forward = (
                    broad_forward
                    - float(secondary_primary_beta)
                    * math.log(benchmark_exit / benchmark_entry)
                )
                model_adjusted = (
                    beta_adjusted - float(secondary_beta) * secondary_factor_forward
                )
        elif (
            beta_adjusted is not None
            and secondary_symbol
            and secondary_beta is not None
            and secondary_primary_beta is not None
        ):
            secondary_entry = closes_by_symbol.get(secondary_symbol, {}).get(entry_open)
            secondary_exit = closes_by_symbol.get(secondary_symbol, {}).get(exit_open)
            if secondary_entry is not None and secondary_exit is not None:
                secondary_factor_forward = (
                    math.log(secondary_exit / secondary_entry)
                    - float(secondary_primary_beta)
                    * math.log(benchmark_exit / benchmark_entry)
                )
                model_adjusted = (
                    beta_adjusted
                    - float(secondary_beta) * secondary_factor_forward
                )
            else:
                model_adjusted = None
        exit_timestamp = exit_open + timedelta(seconds=step)
        funding = (
            funding_cost(symbol, timestamp, exit_timestamp, direction)
            if funding_cost else 0.0
        )
        events.append(EventOutcome(
            timestamp=timestamp,
            symbol=symbol,
            direction=direction,
            gross_forward_return=asset_forward_return,
            event_rule=event_rule,
            factor_model=factor_model,
            funding_cost=funding,
            score=score,
            entry_price=entry_price,
            exit_price=exit_price,
            horizon_bars=horizon_bars,
            benchmark_forward_return=benchmark_forward_return,
            btc_beta_adjusted_forward_log_return=beta_adjusted,
            model_factor_adjusted_forward_log_return=model_adjusted,
            features=features,
        ))
    return events


def generate_point_in_time_events(
    assets: Mapping[str, AssetInput],
    scanner_config: ScannerConfig,
    horizon_bars: int,
    universe_size: Optional[int] = None,
    context_at: Optional[ContextAt] = None,
    funding_cost: Optional[FundingCost] = None,
    event_rule: str = "confirmed_candidate",
    factor_model: str = CURRENT_FACTOR_MODEL,
) -> list[EventOutcome]:
    """Generate one frozen rule while preserving the original public API."""
    return _generate_point_in_time_events_for_rules(
        assets=assets,
        scanner_config=scanner_config,
        horizon_bars=horizon_bars,
        universe_size=universe_size,
        context_at=context_at,
        funding_cost=funding_cost,
        event_rules=(event_rule,),
        factor_model=factor_model,
    )


def generate_point_in_time_event_suite(
    assets: Mapping[str, AssetInput],
    scanner_config: ScannerConfig,
    horizon_bars: int,
    event_rules: Sequence[str],
    universe_size: Optional[int] = None,
    context_at: Optional[ContextAt] = None,
    funding_cost: Optional[FundingCost] = None,
    factor_model: str = CURRENT_FACTOR_MODEL,
) -> dict[str, list[EventOutcome]]:
    """Evaluate several rules from one point-in-time scan per timestamp."""
    ordered_rules = tuple(dict.fromkeys(event_rules))
    events = _generate_point_in_time_events_for_rules(
        assets=assets,
        scanner_config=scanner_config,
        horizon_bars=horizon_bars,
        universe_size=universe_size,
        context_at=context_at,
        funding_cost=funding_cost,
        event_rules=ordered_rules,
        factor_model=factor_model,
    )
    return {
        event_rule: [
            event for event in events if event.event_rule == event_rule
        ]
        for event_rule in ordered_rules
    }


def net_returns(
    events: Iterable[EventOutcome],
    round_trip_cost_bps: float,
    outcome: str = "asset",
) -> list[float]:
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be non-negative")
    if outcome not in {"asset", "btc_beta_adjusted", "model_factor_adjusted"}:
        raise ValueError(
            "outcome must be 'asset', 'btc_beta_adjusted', or "
            "'model_factor_adjusted'"
        )
    cost = round_trip_cost_bps / 10_000.0
    values = []
    for event in events:
        try:
            if outcome == "asset":
                value = event.signed_gross_return
            elif outcome == "btc_beta_adjusted":
                value = event.signed_btc_beta_adjusted_return
            else:
                value = event.signed_model_factor_adjusted_return
        except ValueError:
            continue
        values.append(value - cost - event.funding_cost)
    return values


def evaluate(
    events: Sequence[EventOutcome],
    round_trip_cost_bps: float,
    periods_per_year: float | None = None,
    outcome: str = "asset",
) -> ResearchMetrics:
    """Evaluate a pre-generated event set; it does not select a rule or threshold."""
    returns = net_returns(events, round_trip_cost_bps, outcome=outcome)
    if not returns:
        return ResearchMetrics(0, None, None, None, None, None, None, None, None)
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    sharpe = None
    if len(returns) >= 2:
        deviation = statistics.stdev(returns)
        if deviation > 0:
            annualizer = math.sqrt(periods_per_year) if periods_per_year else 1.0
            sharpe = statistics.fmean(returns) / deviation * annualizer
    return ResearchMetrics(
        count=len(returns), win_rate=len(wins) / len(returns),
        average_win=statistics.fmean(wins) if wins else None,
        average_loss=statistics.fmean(losses) if losses else None,
        expectancy=statistics.fmean(returns),
        profit_factor=(gross_profit / gross_loss if gross_loss else None),
        sharpe=sharpe, max_drawdown=max_drawdown, total_return=equity - 1.0,
    )


def chronological_split(events: Sequence[EventOutcome], train_fraction: float = 0.65) -> tuple[list[EventOutcome], list[EventOutcome]]:
    """One explicit chronological split; callers should also run rolling folds."""
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be in (0, 1)")
    ordered = sorted(events, key=lambda event: event.timestamp)
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * train_fraction))) if len(ordered) > 1 else len(ordered)
    return ordered[:cut], ordered[cut:]


def score_buckets(events: Sequence[EventOutcome], edges: Sequence[float]) -> dict[str, list[EventOutcome]]:
    """Bucket frozen events by absolute score without trying alternate cut-offs."""
    if sorted(edges) != list(edges):
        raise ValueError("bucket edges must be sorted")
    buckets: dict[str, list[EventOutcome]] = {}
    for event in events:
        if event.score is None:
            label = "missing"
        else:
            magnitude = abs(event.score)
            lower = 0.0
            label = f">={edges[-1]:g}"
            for edge in edges:
                if magnitude < edge:
                    label = f"{lower:g}-{edge:g}"
                    break
                lower = edge
        buckets.setdefault(label, []).append(event)
    return buckets


def feature_buckets(
    events: Sequence[EventOutcome], feature: str, edges: Sequence[float]
) -> dict[str, list[EventOutcome]]:
    """Predeclared buckets for a research-only context feature."""
    if sorted(edges) != list(edges):
        raise ValueError("bucket edges must be sorted")
    buckets: dict[str, list[EventOutcome]] = {}
    for event in events:
        value = event.features.get(feature)
        if value is None:
            label = "missing"
        else:
            lower = float("-inf")
            label = f">={edges[-1]:g}" if edges else "all"
            for edge in edges:
                if value < edge:
                    label = f"<{edge:g}" if lower == float("-inf") else f"{lower:g}-{edge:g}"
                    break
                lower = edge
        buckets.setdefault(label, []).append(event)
    return buckets
