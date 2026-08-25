from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from terra_cpr.data import synthetic_demo_assets
from terra_cpr.research import (
    EventOutcome,
    chronological_split,
    evaluate,
    feature_buckets,
    generate_point_in_time_events,
    score_buckets,
)
from terra_cpr.scanner import ScannerConfig


class ResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.events = [
            EventOutcome(start + timedelta(hours=index), "SOL", "LONG", value, score=score)
            for index, (value, score) in enumerate(((0.03, 5.0), (-0.01, 4.0), (0.02, 6.0), (-0.005, 2.0)))
        ]

    def test_costs_are_included_in_expectancy(self) -> None:
        metrics = evaluate(self.events, round_trip_cost_bps=10)
        self.assertEqual(metrics.count, 4)
        self.assertAlmostEqual(metrics.expectancy or 0.0, 0.00775)
        self.assertIsNotNone(metrics.max_drawdown)

    def test_beta_adjusted_outcome_is_a_separate_evaluation_target(self) -> None:
        event = EventOutcome(
            self.events[0].timestamp,
            "SOL",
            "LONG",
            gross_forward_return=0.05,
            btc_beta_adjusted_forward_log_return=0.02,
            model_factor_adjusted_forward_log_return=0.015,
        )
        asset = evaluate([event], round_trip_cost_bps=10, outcome="asset")
        residual = evaluate(
            [event], round_trip_cost_bps=10, outcome="btc_beta_adjusted"
        )
        model_residual = evaluate(
            [event], round_trip_cost_bps=10, outcome="model_factor_adjusted"
        )
        self.assertAlmostEqual(asset.expectancy or 0.0, 0.049)
        self.assertAlmostEqual(residual.expectancy or 0.0, 0.0192013400267558)
        self.assertAlmostEqual(model_residual.expectancy or 0.0, 0.0141130646157189)

    def test_chronological_split_does_not_shuffle(self) -> None:
        train, test = chronological_split(list(reversed(self.events)), train_fraction=0.5)
        self.assertEqual(len(train), 2)
        self.assertLess(train[-1].timestamp, test[0].timestamp)

    def test_score_buckets_are_descriptive(self) -> None:
        buckets = score_buckets(self.events, [3.0, 5.0])
        self.assertEqual(len(buckets["0-3"]), 1)
        self.assertEqual(len(buckets[">=5"]), 2)

    def test_point_in_time_generator_uses_completed_closes_and_adds_outcomes(self) -> None:
        _, interval, assets = synthetic_demo_assets()
        events = generate_point_in_time_events(
            assets,
            ScannerConfig(interval_seconds=interval),
            horizon_bars=4,
        )
        self.assertGreater(len(events), 0)
        closes = {
            symbol: {bar.timestamp: bar.close for bar in asset.intraday}
            for symbol, asset in assets.items()
        }
        for event in events:
            entry_open = event.timestamp - timedelta(seconds=interval)
            exit_open = entry_open + timedelta(seconds=4 * interval)
            self.assertEqual(event.entry_price, closes[event.symbol][entry_open])
            self.assertEqual(event.exit_price, closes[event.symbol][exit_open])
            self.assertEqual(event.horizon_bars, 4)
            self.assertIsNotNone(event.benchmark_forward_return)
            self.assertIsNotNone(event.btc_beta_adjusted_forward_log_return)
            self.assertIsNotNone(event.model_factor_adjusted_forward_log_return)
            self.assertIn("relative_notional_volume", event.features)
            self.assertIn("positive_rs_breadth", event.features)

    def test_feature_buckets_keep_missing_values_explicit(self) -> None:
        events = [
            EventOutcome(
                self.events[0].timestamp,
                "A",
                "LONG",
                0.01,
                features={"relative_notional_volume": 1.5},
            ),
            EventOutcome(
                self.events[1].timestamp,
                "B",
                "LONG",
                0.01,
                features={"relative_notional_volume": None},
            ),
        ]
        buckets = feature_buckets(events, "relative_notional_volume", [1.0, 2.0])
        self.assertEqual(len(buckets["1-2"]), 1)
        self.assertEqual(len(buckets["missing"]), 1)
