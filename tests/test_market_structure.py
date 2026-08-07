from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from terra_cpr.market_structure import (
    MarketStructureConfig,
    build_market_structure,
    calculate_cpr,
    calculate_floor_pivots,
)
from terra_cpr.models import Candle


UTC = timezone.utc


def candle(day: int, close: float, high: float | None = None, low: float | None = None) -> Candle:
    high = high if high is not None else close * 1.02
    low = low if low is not None else close * 0.98
    return Candle(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day), close, high, low, close, 100.0)


class MarketStructureTests(unittest.TestCase):
    def test_conventional_cpr_and_pivots(self) -> None:
        cpr = calculate_cpr(110.0, 90.0, 105.0)
        pivots = calculate_floor_pivots(110.0, 90.0, 105.0)
        self.assertAlmostEqual(cpr.pivot, 101.6666666667)
        self.assertAlmostEqual(cpr.bottom, 100.0)
        self.assertAlmostEqual(cpr.top, 103.3333333333)
        self.assertAlmostEqual(pivots.r1, 113.3333333333)
        self.assertAlmostEqual(pivots.s1, 93.3333333333)

    def test_active_levels_use_last_completed_daily_candle(self) -> None:
        daily = [candle(index, 100.0 + index) for index in range(45)]
        expected = calculate_cpr(daily[-1].high, daily[-1].low, daily[-1].close)
        structure = build_market_structure(
            "SOL", daily, price=150.0, session_open=145.0,
            as_of=datetime(2026, 2, 20, tzinfo=UTC),
            config=MarketStructureConfig(minimum_width_history=20),
        )
        self.assertEqual(structure.active_cpr, expected)
        self.assertEqual(structure.price_cpr_position, "above_tc")
        self.assertEqual(structure.opening_cpr_position, "above_tc")
        self.assertIsNotNone(structure.atr)
        self.assertIsNotNone(structure.cpr_width_percentile)

    def test_duplicate_daily_timestamp_is_rejected(self) -> None:
        first = candle(0, 100.0)
        with self.assertRaises(ValueError):
            build_market_structure(
                "SOL", [first, first], 100.0, None,
                datetime(2026, 1, 3, tzinfo=UTC),
            )
