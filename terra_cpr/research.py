"""Transparent event-study metrics for frozen scanner hypotheses."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence


@dataclass(frozen=True)
class EventOutcome:
    timestamp: datetime
    symbol: str
    direction: str  # LONG or SHORT
    gross_forward_return: float  # decimal asset return over a predetermined horizon
    funding_cost: float = 0.0
    score: float | None = None

    @property
    def signed_gross_return(self) -> float:
        if self.direction == "LONG":
            return self.gross_forward_return
        if self.direction == "SHORT":
            return -self.gross_forward_return
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


def net_returns(events: Iterable[EventOutcome], round_trip_cost_bps: float) -> list[float]:
    if round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be non-negative")
    cost = round_trip_cost_bps / 10_000.0
    return [event.signed_gross_return - cost - event.funding_cost for event in events]


def evaluate(events: Sequence[EventOutcome], round_trip_cost_bps: float, periods_per_year: float | None = None) -> ResearchMetrics:
    """Evaluate a pre-generated event set; it does not select a rule or threshold."""
    returns = net_returns(events, round_trip_cost_bps)
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
