from __future__ import annotations

import json
import math
import statistics
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from terra_cpr.config import load_scanner_config
from terra_cpr.data import HyperliquidPublicData, completed_bars, load_fixture, synthetic_demo_assets
from terra_cpr.models import Candle
from terra_cpr.relative_strength import RSConfig
from terra_cpr.scanner import (
    BROAD_ALT_FACTOR,
    AssetInput,
    ScannerConfig,
    _row_order,
    assess_setup,
    build_broad_alt_factors,
    scan_assets,
)
from terra_cpr.report import scan_snapshot


class DataAndScannerTests(unittest.TestCase):
    def test_non_finite_candle_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Candle(datetime(2026, 1, 1, tzinfo=timezone.utc), float("nan"), 101, 99, 100)

    def test_provider_candle_symbol_mismatch_is_rejected(self) -> None:
        source = HyperliquidPublicData()
        source._post = lambda _payload: [{
            "t": 1_767_225_600_000, "s": "ETH", "i": "1h",
            "o": "100", "h": "101", "l": "99", "c": "100", "v": "1",
        }]
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.assertRaisesRegex(RuntimeError, "symbol mismatch"):
            source.fetch_candles("BTC", "1h", start, start + timedelta(hours=1))

    def test_forming_bar_is_excluded(self) -> None:
        as_of = datetime(2026, 1, 2, 1, 30, tzinfo=timezone.utc)
        closed = Candle(as_of - timedelta(hours=2), 100, 101, 99, 100)
        forming = Candle(as_of - timedelta(minutes=30), 100, 101, 99, 100)
        self.assertEqual(completed_bars([closed, forming], 3600, as_of), [closed])

    def test_duplicate_bar_is_an_explicit_error(self) -> None:
        as_of = datetime(2026, 1, 2, 1, 30, tzinfo=timezone.utc)
        bar = Candle(as_of - timedelta(hours=2), 100, 101, 99, 100)
        with self.assertRaises(ValueError):
            completed_bars([bar, bar], 3600, as_of)

    def test_fixture_loader_filters_incomplete_daily_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            payload = {
                "as_of": "2026-01-03T01:00:00Z", "interval_seconds": 3600,
                "assets": {
                    "BTC": {"price": 100, "daily": [{"t": "2026-01-01T00:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100}], "intraday": []},
                    "SOL": {"price": 10, "daily": [{"t": "2026-01-02T00:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10}], "intraday": []},
                },
            }
            path.write_text(json.dumps(payload))
            _, _, assets = load_fixture(path)
            self.assertEqual(len(assets["SOL"].daily), 1)

    def test_demo_produces_ranked_explainable_rows(self) -> None:
        as_of, interval, assets = synthetic_demo_assets()
        rows = scan_assets(assets, as_of, ScannerConfig(interval_seconds=interval))
        self.assertEqual({row.symbol for row in rows}, {"ETH", "SOL"})
        self.assertTrue(all(row.rs.score is not None for row in rows))
        self.assertEqual({row.strong_rank for row in rows}, {1, 2})
        snapshot = scan_snapshot(as_of, rows)
        self.assertIn("bottom", snapshot["rows"][0]["market"]["active_cpr"])

    def test_config_rejects_a_timeframe_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scanner.toml"
            path.write_text("[scanner]\ninterval_seconds = 900\n")
            with self.assertRaises(ValueError):
                load_scanner_config(path, 3600)

    def test_config_derives_duration_preserving_windows_for_15_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scanner.toml"
            path.write_text("[scanner]\ninterval_seconds = 900\n")
            config = load_scanner_config(path, 900)
        self.assertEqual(config.rs.bar_interval_seconds, 900)
        self.assertEqual(config.rs.short_horizon_bars, 16)
        self.assertEqual(config.rs.persistence_window, 96)

    def test_stale_scores_do_not_receive_cross_sectional_ranks(self) -> None:
        as_of, interval, assets = synthetic_demo_assets()
        rows = scan_assets(
            assets, as_of + timedelta(hours=5), ScannerConfig(interval_seconds=interval)
        )
        self.assertTrue(all("stale_or_misaligned" in row.rs.quality_flags for row in rows))
        self.assertTrue(all(row.strong_rank is None and row.weak_rank is None for row in rows))

    def test_zero_score_sorts_ahead_of_negative_score(self) -> None:
        zero = SimpleNamespace(symbol="ZERO", strong_rank=1, rs=SimpleNamespace(score=0.0))
        negative = SimpleNamespace(symbol="NEG", strong_rank=2, rs=SimpleNamespace(score=-1.0))
        self.assertEqual(sorted([negative, zero], key=_row_order), [zero, negative])

    def test_mid_cross_is_provisional_until_a_completed_close_confirms(self) -> None:
        market = SimpleNamespace(
            active_cpr=SimpleNamespace(top=110.0, bottom=100.0),
            pivots=SimpleNamespace(pivot=105.0, r1=115.0, s1=95.0),
        )
        rs = SimpleNamespace(is_usable=True, score=5.0, persistence=0.6)
        provisional = assess_setup(
            "ALT", 112.0, 109.0, market, rs, ScannerConfig(),
            confirmed_price=108.0, confirmed_prior_price=107.0,
        )
        confirmed = assess_setup(
            "ALT", 112.0, 109.0, market, rs, ScannerConfig(),
            confirmed_price=111.0, confirmed_prior_price=108.0,
        )
        self.assertEqual(provisional.confirmation, "PROVISIONAL")
        self.assertEqual(confirmed.confirmation, "CONFIRMED")

    def test_discovery_tiers_use_persistence_free_score_without_creating_candidates(self) -> None:
        market = SimpleNamespace(
            active_cpr=SimpleNamespace(top=110.0, bottom=100.0),
            pivots=SimpleNamespace(pivot=105.0, r1=115.0, s1=95.0),
        )
        early_rs = SimpleNamespace(
            is_usable=True, score=1.0, discovery_score=2.7,
            persistence=-1.0, quality_flags=(), reason=None,
        )
        strong_rs = SimpleNamespace(
            is_usable=True, score=1.0, discovery_score=-3.2,
            persistence=1.0, quality_flags=(), reason=None,
        )
        early = assess_setup("EARLY", 112.0, 111.0, market, early_rs, ScannerConfig())
        strong = assess_setup("STRONG", 98.0, 99.0, market, strong_rs, ScannerConfig())
        self.assertEqual((early.label, early.direction, early.discovery_tier), ("WATCH", "LONG", "EARLY_DISCOVERY"))
        self.assertEqual((strong.label, strong.direction, strong.discovery_tier), ("WATCH", "SHORT", "STRONG_DISCOVERY"))
        self.assertEqual(early.confirmation, "NONE")
        self.assertEqual(strong.confirmation, "NONE")

    def test_discovery_thresholds_are_ordered_below_the_candidate_gate(self) -> None:
        with self.assertRaisesRegex(ValueError, "early <= strong <= candidate"):
            ScannerConfig(early_discovery_score=3.1, strong_discovery_score=3.0)

    def test_broad_alt_factor_is_equal_weight_and_leave_one_out(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def bars(symbol_return: float) -> list[Candle]:
            price = 100.0
            output = []
            for index in range(3):
                if index:
                    price *= 1.0 + symbol_return
                timestamp = start + timedelta(hours=index)
                output.append(Candle(timestamp, price, price, price, price, 1.0))
            return output

        assets = {
            symbol: AssetInput(symbol, series[-1].close, (), series)
            for symbol, series in {
                "BTC": bars(0.0), "A": bars(0.01),
                "B": bars(0.02), "C": bars(0.03),
            }.items()
        }
        factors = build_broad_alt_factors(assets, "BTC", 3600, 2)
        a_bars, a_count = factors["A"]
        b_bars, b_count = factors["B"]
        expected_a = 2 * statistics.fmean((math.log(1.02), math.log(1.03)))
        expected_b = 2 * statistics.fmean((math.log(1.01), math.log(1.03)))
        self.assertAlmostEqual(math.log(a_bars[-1].close / a_bars[0].close), expected_a)
        self.assertAlmostEqual(math.log(b_bars[-1].close / b_bars[0].close), expected_b)
        self.assertEqual((a_count, b_count), (2, 2))
        self.assertNotEqual(a_bars[-1].close, b_bars[-1].close)

    def test_broad_alt_factor_refuses_an_undersized_cross_section(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        bars = [
            Candle(start + timedelta(hours=index), 100 + index, 100 + index,
                   100 + index, 100 + index, 1.0)
            for index in range(3)
        ]
        assets = {
            symbol: AssetInput(symbol, bars[-1].close, (), bars)
            for symbol in ("BTC", "A", "B", "C")
        }
        self.assertEqual(build_broad_alt_factors(assets, "BTC", 3600, 3), {})

    def test_broad_alt_factor_restarts_after_a_cross_section_gap(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)

        def bars(missing: frozenset[int] = frozenset()) -> list[Candle]:
            return [
                Candle(
                    start + timedelta(hours=index), 100 + index, 100 + index,
                    100 + index, 100 + index, 1.0,
                )
                for index in range(6) if index not in missing
            ]

        assets = {
            "BTC": AssetInput("BTC", 105, (), bars()),
            "A": AssetInput("A", 105, (), bars()),
            "B": AssetInput("B", 105, (), bars(frozenset({2}))),
            "C": AssetInput("C", 105, (), bars(frozenset({2}))),
        }
        factor_bars, _count = build_broad_alt_factors(
            assets, "BTC", 3600, min_constituents=2
        )["A"]
        self.assertEqual(
            [bar.timestamp for bar in factor_bars],
            [start + timedelta(hours=index) for index in (3, 4, 5)],
        )

    def test_scanner_can_use_the_broad_alt_factor_without_touching_live_defaults(self) -> None:
        as_of, interval, assets = synthetic_demo_assets()
        broad_rs = replace(
            RSConfig.for_interval(interval),
            secondary_benchmark=BROAD_ALT_FACTOR,
            broad_alt_min_constituents=1,
        )
        rows = scan_assets(
            assets, as_of,
            ScannerConfig(interval_seconds=interval, rs=broad_rs),
        )
        self.assertTrue(all(row.rs.beta is not None for row in rows))
        self.assertTrue(all(
            row.rs.beta.secondary_benchmark == BROAD_ALT_FACTOR
            for row in rows if row.rs.beta is not None
        ))
        self.assertTrue(all(row.rs.secondary_factor_constituents == 1 for row in rows))
        self.assertTrue(all("broad_alt_l1o" in row.rs.model_version for row in rows))
        self.assertEqual(RSConfig().secondary_benchmark, "ETH")


class AtomicWriteTests(unittest.TestCase):
    """A failed publish must not sabotage the next one.

    The writer used one fixed ``<name>.tmp`` path and left it in place when the
    rename failed. On macOS the abandoned file kept the ``com.apple.macl`` tag
    TCC had stamped on it, so every later ``os.replace`` onto the real snapshot
    was denied too: one transient permission error turned into a permanently
    frozen dashboard that outlived a process restart.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.target = self.directory / "scanner_latest.json"

    def _strays(self) -> list[Path]:
        return [p for p in self.directory.iterdir() if p != self.target]

    def test_a_successful_write_leaves_no_temporary_behind(self) -> None:
        from terra_cpr.report import write_json_atomic

        write_json_atomic(self.target, {"ok": True})
        self.assertEqual(json.loads(self.target.read_text()), {"ok": True})
        self.assertEqual(self._strays(), [])

    def test_the_published_snapshot_keeps_an_ordinary_readable_mode(self) -> None:
        """mkstemp creates 0600 and os.replace preserves the source mode, so an
        unguarded temp-file swap silently tightens what it publishes."""
        import stat

        from terra_cpr.report import write_json_atomic

        write_json_atomic(self.target, {"ok": True})
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o644)

    def test_a_failed_rename_leaves_no_temporary_to_poison_the_next_write(self) -> None:
        import os as os_module

        from terra_cpr import report

        original = report.os.replace

        def deny(*_args, **_kwargs):
            raise PermissionError(1, "Operation not permitted")

        report.os.replace = deny
        try:
            with self.assertRaises(PermissionError):
                report.write_json_atomic(self.target, {"attempt": 1})
        finally:
            report.os.replace = original
        self.assertEqual(self._strays(), [], "a stray temp file survived a failed rename")

        report.write_json_atomic(self.target, {"attempt": 2})
        self.assertEqual(json.loads(self.target.read_text()), {"attempt": 2})
        self.assertEqual(os_module.path.exists(self.target), True)

    def test_a_denied_cleanup_does_not_mask_why_the_write_failed(self) -> None:
        """The cleanup runs on the failure path, where the filesystem is often
        exactly what is refusing us. If the unlink is denied too, the caller
        must still learn the real reason -- the health rail shows last_error,
        and a swapped-in cleanup error sends the reader after the wrong fault."""
        from terra_cpr import report

        original_replace, original_unlink = report.os.replace, Path.unlink

        def deny_replace(*_a, **_k):
            raise PermissionError(1, "Operation not permitted: the rename")

        def deny_unlink(*_a, **_k):
            raise PermissionError(1, "Operation not permitted: the cleanup")

        report.os.replace = deny_replace
        Path.unlink = deny_unlink
        try:
            with self.assertRaises(PermissionError) as caught:
                report.write_json_atomic(self.target, {"attempt": 1})
        finally:
            report.os.replace, Path.unlink = original_replace, original_unlink
        self.assertIn("the rename", str(caught.exception))
        self.assertNotIn("the cleanup", str(caught.exception))

    def test_concurrent_writers_do_not_share_one_temporary_path(self) -> None:
        """Two writers colliding on a fixed temp name can publish a torn file."""
        from terra_cpr import report

        seen: list[str] = []
        original = report.os.replace

        def record(source, destination):
            seen.append(str(source))
            return original(source, destination)

        report.os.replace = record
        try:
            report.write_json_atomic(self.target, {"n": 1})
            report.write_json_atomic(self.target, {"n": 2})
        finally:
            report.os.replace = original
        self.assertEqual(len(set(seen)), 2, f"temp path was reused across writes: {seen}")
