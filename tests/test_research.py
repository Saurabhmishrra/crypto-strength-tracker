from __future__ import annotations

import math
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from terra_cpr.data import synthetic_demo_assets
from terra_cpr.research import (
    BROAD_ALT_FACTOR_MODEL,
    FIVE_MODEL_COMPARISON,
    _broad_alt_forward_log_return,
    EventOutcome,
    chronological_split,
    evaluate,
    feature_buckets,
    generate_point_in_time_events,
    generate_point_in_time_event_suite,
    score_buckets,
)
from terra_cpr.scanner import ScannerConfig
from terra_cpr.relative_strength import RSConfig
from terra_cpr.cli import _write_backtest


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
        self.assertIsNone(metrics.max_drawdown)
        self.assertIsNone(metrics.total_return)
        self.assertIsNone(metrics.sharpe)
        self.assertIsNotNone(metrics.event_mean_stdev_ratio)

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
            self.assertEqual(event.event_rule, "confirmed_candidate")
            self.assertIsNotNone(event.benchmark_forward_return)
            self.assertIsNotNone(event.btc_beta_adjusted_forward_log_return)
            self.assertIsNotNone(event.model_factor_adjusted_forward_log_return)
            self.assertIn("relative_notional_volume", event.features)
            self.assertIn("positive_rs_breadth", event.features)
            self.assertIn("discovery_score", event.features)

    def test_discovery_event_rule_uses_the_persistence_free_threshold(self) -> None:
        _, interval, assets = synthetic_demo_assets()
        events = generate_point_in_time_events(
            assets,
            ScannerConfig(interval_seconds=interval),
            horizon_bars=4,
            event_rule="early_discovery",
        )
        self.assertGreater(len(events), 0)
        self.assertTrue(all(event.event_rule == "early_discovery" for event in events))
        self.assertTrue(all(abs(event.score or 0.0) >= 2.5 for event in events))

    def test_h5_is_strong_discovery_conditioned_on_completed_structure(self) -> None:
        _, interval, assets = synthetic_demo_assets()
        config = ScannerConfig(interval_seconds=interval)
        h5 = generate_point_in_time_events(
            assets, config, horizon_bars=4,
            event_rule="h5_discovery_structure",
        )
        self.assertGreater(len(h5), 0)
        for event in h5:
            self.assertEqual(event.event_rule, "h5_discovery_structure")
            self.assertGreaterEqual(abs(event.score or 0.0), 3.0)
            self.assertEqual(event.features["completed_structure"], 1.0)
            self.assertEqual(event.features["trigger_threshold"], 3.0)

    def test_residual_momentum_baseline_uses_cross_sectional_tail_and_sign(self) -> None:
        _, interval, assets = synthetic_demo_assets()
        events = generate_point_in_time_events(
            assets,
            ScannerConfig(interval_seconds=interval),
            horizon_bars=4,
            event_rule="residual_momentum_baseline",
        )
        self.assertGreater(len(events), 0)
        for event in events:
            percentile = event.features["residual_momentum_percentile"]
            residual = event.features["residual_momentum_24h"]
            self.assertIsNotNone(percentile)
            self.assertIsNotNone(residual)
            assert percentile is not None and residual is not None
            if event.direction == "LONG":
                self.assertGreaterEqual(percentile, 0.80)
                self.assertGreater(residual, 0.0)
            else:
                self.assertLessEqual(percentile, 0.20)
                self.assertLess(residual, 0.0)

    def test_five_model_suite_is_frozen_and_auditable(self) -> None:
        self.assertEqual(
            [spec.spec_id for spec in FIVE_MODEL_COMPARISON],
            ["H1", "D1", "H5", "B1", "H6"],
        )
        h6 = FIVE_MODEL_COMPARISON[-1]
        self.assertEqual(h6.event_rule, "h5_discovery_structure")
        self.assertEqual(h6.factor_model, BROAD_ALT_FACTOR_MODEL)

    def test_multi_rule_generator_keeps_rule_states_separate(self) -> None:
        _, interval, assets = synthetic_demo_assets()
        events = generate_point_in_time_event_suite(
            assets,
            ScannerConfig(interval_seconds=interval),
            horizon_bars=4,
            event_rules=("strong_discovery", "h5_discovery_structure"),
        )
        self.assertEqual(set(events), {"strong_discovery", "h5_discovery_structure"})
        self.assertTrue(all(
            event.event_rule == event_rule
            for event_rule, rule_events in events.items()
            for event in rule_events
        ))
        self.assertGreater(len(events["strong_discovery"]), 0)
        self.assertGreater(len(events["h5_discovery_structure"]), 0)

    def test_broad_alt_forward_outcome_holds_entry_constituents_fixed(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        returns = {"T": 0.04, "A": 0.01, "B": 0.02, "C": 0.03}
        closes = {}
        for symbol, hourly_return in returns.items():
            price = 100.0
            by_time = {start: price}
            for index in (1, 2):
                price *= 1.0 + hourly_return
                by_time[start + timedelta(hours=index)] = price
            closes[symbol] = by_time
        factor_return = _broad_alt_forward_log_return(
            closes, ("T", "A", "B", "C"), "T",
            start, start + timedelta(hours=2), 3600, 3,
        )
        expected = 2 * sum(math.log(1.0 + value) for value in (0.01, 0.02, 0.03)) / 3
        self.assertAlmostEqual(factor_return or 0.0, expected)

    def test_broad_alt_challenger_is_generated_end_to_end(self) -> None:
        _, interval, assets = synthetic_demo_assets()
        config = ScannerConfig(
            interval_seconds=interval,
            rs=replace(
                RSConfig.for_interval(interval),
                broad_alt_min_constituents=1,
            ),
        )
        events = generate_point_in_time_events(
            assets, config, horizon_bars=4,
            event_rule="h5_discovery_structure",
            factor_model=BROAD_ALT_FACTOR_MODEL,
        )
        self.assertGreater(len(events), 0)
        self.assertTrue(all(
            event.factor_model == BROAD_ALT_FACTOR_MODEL
            and event.features["secondary_factor_constituents"] == 1
            and event.model_factor_adjusted_forward_log_return is not None
            for event in events
        ))

    def test_cli_comparison_suite_reports_all_five_frozen_specs(self) -> None:
        fixture = synthetic_demo_assets()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "comparison.json"
            args = SimpleNamespace(
                input=Path("unused.json"), output=output, config=None,
                horizon_bars=[4], universe=None, event_rule=None,
                factor_model=None, comparison_suite=True,
                cost_bps=10.0, train_fraction=0.65,
            )
            with patch("terra_cpr.cli.load_fixture", return_value=fixture):
                _write_backtest(args)
            report = json.loads(output.read_text())
        self.assertEqual(report["schema_version"], 4)
        self.assertEqual(
            [spec["spec_id"] for spec in report["specifications"]],
            ["H1", "D1", "H5", "B1", "H6"],
        )
        self.assertEqual(
            {result["spec_id"] for result in report["results"]},
            {"H1", "D1", "H5", "B1", "H6"},
        )
        self.assertEqual(
            report["comparison_contract"]["primary_selection_metric"],
            "asset_return.test.expectancy",
        )
        self.assertIn(
            "final untouched holdout",
            report["comparison_contract"]["promotion_rule"],
        )

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
