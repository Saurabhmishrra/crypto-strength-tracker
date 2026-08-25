from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from terra_cpr.models import Candle
from terra_cpr.relative_strength import (
    RSConfig,
    _acceleration,
    _empirical_horizon,
    compute_relative_strength,
    estimate_robust_ewma_factor_model,
)


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
        self.assertAlmostEqual(result.beta.beta, 1.8, delta=0.01)
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

    def test_new_listing_is_judged_on_its_overlapping_history(self) -> None:
        alt, btc = panel(beta=1.2, alpha=0.0005)
        alt = alt[-160:]
        result = compute_relative_strength(
            "NEW", alt, "BTC", btc, 3600,
            btc[-1].timestamp + timedelta(hours=1), self.config,
        )
        self.assertAlmostEqual(result.alignment_ratio, 1.0)
        self.assertNotIn("stale_or_misaligned", result.quality_flags)
        self.assertTrue(result.is_usable)

    def test_fixture_history_deeper_than_live_cache_does_not_change_score(self) -> None:
        alt, btc = panel(beta=1.4, alpha=0.0004, count=1000)
        as_of = btc[-1].timestamp + timedelta(hours=1)
        full = compute_relative_strength(
            "ALT", alt, "BTC", btc, 3600, as_of, self.config,
        )
        preferred = max(
            self.config.beta_window,
            self.config.persistence_window,
            self.config.long_horizon_bars + self.config.min_empirical_windows,
        ) + 1
        tail = compute_relative_strength(
            "ALT", alt[-preferred:], "BTC", btc[-preferred:], 3600, as_of,
            self.config,
        )
        self.assertEqual(full.score, tail.score)
        self.assertEqual(full.horizon_z, tail.horizon_z)

    def test_steady_excess_return_is_not_mechanical_deceleration(self) -> None:
        residuals = [0.001] * 24
        self.assertAlmostEqual(_acceleration(residuals, 4, 24, 0.01) or 0.0, 0.0)

    def test_recent_step_up_is_positive_acceleration(self) -> None:
        residuals = [0.001] * 20 + [0.004] * 4
        acceleration = _acceleration(residuals, 4, 24, 0.01)
        self.assertIsNotNone(acceleration)
        assert acceleration is not None
        self.assertGreater(acceleration, 0.0)

    def test_invalid_horizon_order_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RSConfig(short_horizon_bars=24, medium_horizon_bars=4, long_horizon_bars=168)

    def test_persistence_window_requires_its_full_history(self) -> None:
        alt, btc = panel(beta=1.2, alpha=0.0005)
        config = RSConfig(
            beta_window=120, min_beta_points=100, persistence_window=300,
            short_horizon_bars=4, medium_horizon_bars=12, long_horizon_bars=48,
        )
        result = compute_relative_strength(
            "ALT", alt, "BTC", btc, 3600,
            btc[-1].timestamp + timedelta(hours=1), config,
        )
        self.assertIsNone(result.score)
        self.assertIn("need 301", result.reason or "")

    def test_timeframe_profile_preserves_elapsed_horizons(self) -> None:
        config = RSConfig.for_interval(900)
        self.assertEqual(config.short_horizon_bars, 16)
        self.assertEqual(config.medium_horizon_bars, 96)
        self.assertEqual(config.long_horizon_bars, 672)
        self.assertEqual(config.persistence_window, 96)
        self.assertEqual(config.beta_window, 2880)

    def test_robust_ewma_beta_resists_one_extreme_return(self) -> None:
        btc = [0.001 * math.sin(index / 7) for index in range(240)]
        alt = [1.5 * value + 0.0001 * math.cos(index / 5) for index, value in enumerate(btc)]
        alt[180] = 0.50
        estimate = estimate_robust_ewma_factor_model(
            alt, btc, min_points=100, half_life=80, winsor_mad=6.0,
        )
        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertAlmostEqual(estimate.beta, 1.5, delta=0.08)
        self.assertIsNotNone(estimate.beta_standard_error)

    def test_empirical_horizon_scale_is_not_driven_by_one_old_tail(self) -> None:
        history = [-0.002, -0.001, 0.0, 0.001, 0.002] * 20 + [0.50]
        robust_unit, excess, percentile = _empirical_horizon(
            history + [0.004], bars=1, min_windows=60,
        )
        self.assertAlmostEqual(excess or 0.0, 0.004)
        self.assertIsNotNone(robust_unit)
        assert robust_unit is not None
        self.assertGreater(robust_unit, 2.5)
        self.assertLess(robust_unit, 3.0)
        self.assertGreater(percentile or 0.0, 0.9)

    def test_orthogonal_secondary_factor_is_recovered(self) -> None:
        btc = [0.001 * math.sin(index / 7) for index in range(300)]
        eth_factor = [0.0008 * math.cos(index / 11) for index in range(300)]
        alt = [
            1.2 * btc_value + 0.7 * eth_value
            for btc_value, eth_value in zip(btc, eth_factor)
        ]
        estimate = estimate_robust_ewma_factor_model(
            alt, btc, min_points=100, half_life=100, winsor_mad=6.0,
            secondary_returns=eth_factor, secondary_benchmark="ETH",
        )
        self.assertIsNotNone(estimate)
        assert estimate is not None
        self.assertAlmostEqual(estimate.beta, 1.2, delta=0.03)
        self.assertAlmostEqual(estimate.secondary_beta or 0.0, 0.7, delta=0.03)
        self.assertEqual(estimate.secondary_benchmark, "ETH")
