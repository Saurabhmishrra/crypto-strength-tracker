from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from terra_cpr.data import synthetic_demo_assets
from terra_cpr.models import Candle
from terra_cpr.relative_strength import RSConfig
from terra_cpr.research import (
    BROAD_ALT_FACTOR_MODEL,
    CURRENT_FACTOR_MODEL,
    FIVE_MODEL_COMPARISON,
    config_for_factor_model,
)
from terra_cpr.research_archive import ResearchArchive
from terra_cpr.scanner import AssetInput, ScannerConfig, scan_assets


class ResearchArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "research_archive.sqlite3"
        self.as_of, self.interval, self.panel = synthetic_demo_assets()
        self.config = ScannerConfig(
            interval_seconds=self.interval,
            rs=replace(
                RSConfig.for_interval(self.interval),
                broad_alt_min_constituents=1,
            ),
        )
        self.rows = {
            factor_model: scan_assets(
                self.panel,
                self.as_of,
                config_for_factor_model(self.config, factor_model),
            )
            for factor_model in (CURRENT_FACTOR_MODEL, BROAD_ALT_FACTOR_MODEL)
        }

    def _advance_panel(self) -> dict[str, AssetInput]:
        output = {}
        for symbol, asset in self.panel.items():
            prior = asset.intraday[-1]
            candle = Candle(
                prior.timestamp + timedelta(seconds=self.interval),
                prior.close, prior.close, prior.close, prior.close, prior.volume,
            )
            output[symbol] = replace(
                asset,
                price=candle.close,
                prior_price=prior.close,
                intraday=tuple(asset.intraday) + (candle,),
            )
        return output

    def test_first_completed_panel_seeds_all_five_frozen_models(self) -> None:
        archive = ResearchArchive(self.path)
        status = archive.record(self.panel, self.rows, self.config)
        self.assertEqual(status.scans, 1)
        self.assertEqual(status.panel_rows, len(self.panel))
        self.assertGreater(status.candles, 3_000)
        self.assertEqual(status.evaluations, 5)
        self.assertEqual(status.observations, 7)
        self.assertEqual(status.events, 7)
        self.assertEqual(status.active_states, 7)
        self.assertEqual(set(status.models), {"H1", "D1", "H5", "B1", "H6"})
        self.assertTrue(all(
            status.models[spec_id]["evaluations"] == 1
            for spec_id in status.models
        ))
        self.assertTrue(all(
            status.models[spec_id]["observations"] > 0
            for spec_id in status.models
        ))
        self.assertTrue(all(
            status.models[spec_id]["activations"] == 0
            for spec_id in status.models
        ))
        self.assertEqual(
            sum(model["seeded"] for model in status.models.values()), 7
        )

        with sqlite3.connect(self.path) as connection:
            specs = {
                row[0] for row in connection.execute(
                    "SELECT DISTINCT spec_id FROM research_observations"
                )
            }
            event_types = {
                row[0] for row in connection.execute(
                    "SELECT DISTINCT event_type FROM research_events"
                )
            }
        self.assertEqual(specs, {spec.spec_id for spec in FIVE_MODEL_COMPARISON})
        self.assertEqual(event_types, {"seeded"})

    def test_same_completed_bar_is_idempotent(self) -> None:
        archive = ResearchArchive(self.path)
        first = archive.record(self.panel, self.rows, self.config)
        second = archive.record(self.panel, self.rows, self.config)
        self.assertEqual(second.scans, first.scans)
        self.assertEqual(second.candles, first.candles)
        self.assertEqual(second.observations, first.observations)
        self.assertEqual(second.events, first.events)

    def test_active_states_survive_a_process_restart_without_reactivation(self) -> None:
        ResearchArchive(self.path).record(self.panel, self.rows, self.config)
        restarted = ResearchArchive(self.path)
        status = restarted.record(self._advance_panel(), self.rows, self.config)
        self.assertEqual(status.scans, 2)
        self.assertEqual(status.observations, 14)
        self.assertEqual(status.events, 7)

    def test_unavailable_rows_preserve_states_without_false_clearings(self) -> None:
        archive = ResearchArchive(self.path)
        archive.record(self.panel, self.rows, self.config)
        status = archive.record(
            self._advance_panel(),
            {CURRENT_FACTOR_MODEL: (), BROAD_ALT_FACTOR_MODEL: ()},
            self.config,
        )
        self.assertEqual(status.active_states, 7)
        with sqlite3.connect(self.path) as connection:
            cleared = connection.execute(
                "SELECT COUNT(*) FROM research_events WHERE event_type = 'cleared'"
            ).fetchone()[0]
        self.assertEqual(cleared, 0)

    def test_out_of_order_completed_panels_are_rejected(self) -> None:
        archive = ResearchArchive(self.path)
        archive.record(self._advance_panel(), self.rows, self.config)
        with self.assertRaisesRegex(ValueError, "out-of-order"):
            archive.record(self.panel, self.rows, self.config)
