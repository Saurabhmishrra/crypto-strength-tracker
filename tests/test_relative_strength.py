from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from terra_cpr.models import Candle
from terra_cpr.relative_strength import RSConfig, compute_relative_strength


UTC = timezone.utc


def panel(beta: float, alpha: float, count: int = 260) -> tuple[list[Candle], list[Candle]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    btc_log, alt_log = math.log(100_000.0), math.log(100.0)
    btc, alt = [], []
    for index in range(count):
        btc_return = 0.001 * math.sin(index / 5.0) + 0.0002 * math.cos(index / 11.0)
        residual = alpha + 0.00025 * math.sin(index / 3.0)
        btc_log += btc_return
        alt_log += beta * btc_return + residual
        timestamp = start + timedelta(hours=index)
        btc_price, alt_price = math.exp(btc_log), math.exp(alt_log)
        btc.append(Candle(timestamp, btc_price, btc_price * 1.001, btc_price * 0.999, btc_price, 1.0))
        alt.append(Candle(timestamp, alt_price, alt_price * 1.001, alt_price * 0.999, alt_price, 1.0))
    return alt, btc


class RelativeStrengthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RSConfig(
            beta_window=120, min_beta_points=100, persistence_window=20,
            short_horizon_bars=4, medium_horizon_bars=12, long_horizon_bars=48,
        )

    def test_beta_adjustment_recovers_beta_and_persistent_alpha(self) -> None:
        alt, btc = panel(beta=1.8, alpha=0.0008)
        result = compute_relative_strength("SOL", alt, "BTC", btc, 3600, btc[-1].timestamp + timedelta(hours=1), self.config)
        self.assertIsNotNone(result.beta)
        assert result.beta is not None
        self.assertAlmostEqual(result.beta.beta, 1.8, places=2)
        self.assertIsNotNone(result.score)
        assert result.score is not None
        self.assertGreater(result.score, 3.0)
        self.assertGreater(result.persistence or 0.0, 0.4)

    def test_high_beta_without_residual_strength_is_not_ranked_strong(self) -> None:
        alt, btc = panel(beta=2.2, alpha=0.0)
        result = compute_relative_strength("ALT", alt, "BTC", btc, 3600, btc[-1].timestamp + timedelta(hours=1), self.config)
        self.assertTrue(result.score is None or result.score < 1.0)

    def test_stale_bars_are_flagged_instead_of_silently_scored(self) -> None:
        alt, btc = panel(beta=1.2, alpha=0.0005)
        result = compute_relative_strength("ALT", alt, "BTC", btc, 3600, btc[-1].timestamp + timedelta(hours=5), self.config)
        self.assertIn("stale_or_misaligned", result.quality_flags)
        self.assertFalse(result.is_usable)

    def test_missing_timestamp_breaks_alignment(self) -> None:
        alt, btc = panel(beta=1.2, alpha=0.0005)
        alt.pop(-10)
        result = compute_relative_strength("ALT", alt, "BTC", btc, 3600, btc[-1].timestamp + timedelta(hours=1), self.config)
        self.assertIn("stale_or_misaligned", result.quality_flags)
