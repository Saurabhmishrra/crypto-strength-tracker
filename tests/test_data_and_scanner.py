from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from terra_cpr.config import load_scanner_config
from terra_cpr.data import completed_bars, load_fixture, synthetic_demo_assets
from terra_cpr.models import Candle
from terra_cpr.scanner import ScannerConfig, scan_assets
from terra_cpr.report import scan_snapshot


class DataAndScannerTests(unittest.TestCase):
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
