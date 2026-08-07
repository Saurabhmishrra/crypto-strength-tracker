from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from terra_cpr.research import EventOutcome, chronological_split, evaluate, score_buckets


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

    def test_chronological_split_does_not_shuffle(self) -> None:
        train, test = chronological_split(list(reversed(self.events)), train_fraction=0.5)
        self.assertEqual(len(train), 2)
        self.assertLess(train[-1].timestamp, test[0].timestamp)

    def test_score_buckets_are_descriptive(self) -> None:
        buckets = score_buckets(self.events, [3.0, 5.0])
        self.assertEqual(len(buckets["0-3"]), 1)
        self.assertEqual(len(buckets[">=5"]), 2)
