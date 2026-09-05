"""Regression coverage for the Astra review's timing and durability defects."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from terra_cpr.archive_replay import archived_panels, replay_archive, write_replay
from terra_cpr.config import load_live_configs, load_scanner_config, _read_toml
from terra_cpr.data import synthetic_demo_assets
from terra_cpr.live import CandleCache, LiveConfig, LiveScanner, build_panel
from terra_cpr.models import Candle, MarketContext
from terra_cpr.relative_strength import RSConfig
from terra_cpr.report import scan_snapshot, _json_default
from terra_cpr.research import (
    CURRENT_FACTOR_MODEL, BROAD_ALT_FACTOR_MODEL, EventOutcome,
    active_research_triggers, chronological_split, evaluate,
    config_for_factor_model, generate_point_in_time_events,
)
from terra_cpr.research_archive import ResearchArchive
from terra_cpr.research_reporting import calendar_plan, load_funding_series, summarize_events
from terra_cpr.scanner import ScannerConfig, scan_assets
from terra_cpr.signal_history import SignalStore, read_history

NOW, STEP, PANEL = synthetic_demo_assets()
CONFIG = ScannerConfig(rs=replace(RSConfig(), broad_alt_min_constituents=1))


class Source:
    def fetch_meta_and_contexts(self):
        return {"universe": [{"name": s} for s in PANEL]}, [{"dayNtlVlm": "1000"} for _ in PANEL]

    def fetch_mids(self):
        return {s: a.price for s, a in PANEL.items()}

    def fetch_candles(self, symbol, interval, start, end):
        return list(PANEL[symbol].daily if interval == "1d" else PANEL[symbol].intraday)


def factor_rows(panel=PANEL, now=NOW):
    return {m: scan_assets(panel, now, config_for_factor_model(CONFIG, m)) for m in (CURRENT_FACTOR_MODEL, BROAD_ALT_FACTOR_MODEL)}


def advance(panel=PANEL):
    result = {}
    for symbol, asset in panel.items():
        candle = asset.intraday[-1]
        new = Candle(candle.timestamp + timedelta(seconds=STEP), candle.close, candle.close, candle.close, candle.close)
        result[symbol] = replace(asset, intraday=[*asset.intraday, new])
    return result


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)

    def scanner(self):
        return LiveScanner(self.output, Source(), CONFIG, LiveConfig(min_gap_seconds=0), now=lambda: NOW)

    def test_clock_cannot_finalize_partial_hour_or_day(self):
        for interval in (STEP, 86400):
            with self.subTest(interval=interval):
                cache = CandleCache(10)
                start = NOW.replace(hour=0) if interval == 86400 else NOW
                partial = Candle(start, 100, 110, 90, 105)
                cache.merge("SOL", [partial], observed_at=start + timedelta(seconds=30), interval_seconds=interval)
                later = start + timedelta(seconds=interval + 10)
                self.assertEqual(cache.completed("SOL", interval, later), [])
                final = replace(partial, close=101)
                cache.merge("SOL", [final], observed_at=later, interval_seconds=interval)
                self.assertEqual(cache.completed("SOL", interval, later), [final])

    def test_fast_panel_uses_verified_close_before_settlement(self):
        cache = CandleCache(1100)
        for symbol, asset in PANEL.items():
            partial = Candle(NOW, asset.price, asset.price, asset.price, asset.price)
            cache.merge(symbol, [*asset.intraday, partial], observed_at=NOW + timedelta(seconds=30), interval_seconds=STEP)
        panel = build_panel(list(PANEL), cache, {s: a.daily for s, a in PANEL.items()}, Source().fetch_mids(), {}, NOW + timedelta(hours=1, seconds=10), STEP)
        self.assertLess(panel["SOL"].intraday[-1].timestamp, NOW)

    def test_eviction_removes_verification_metadata(self):
        cache = CandleCache(1)
        cache.merge("SOL", PANEL["SOL"].intraday)
        self.assertEqual(len(cache._verified["SOL"]), 1)

    def test_stale_rows_cannot_enter_any_discovery_rule(self):
        rows = scan_assets(PANEL, NOW + timedelta(hours=3), CONFIG)
        self.assertTrue(all(not r.rs.is_usable for r in rows))
        rules = ("early_discovery", "strong_discovery", "h5_discovery_structure")
        self.assertTrue(all(not value for value in active_research_triggers(rows, CONFIG, rules).values()))

    def test_discovery_uses_completed_price_even_for_watch(self):
        rows = scan_assets(PANEL, NOW, CONFIG)
        for row in rows:
            self.assertEqual(row.setup.confirmation_price, PANEL[row.symbol].intraday[-1].close)

    def test_stale_daily_blocks_confirmation(self):
        panel = {s: replace(a, daily=a.daily[:-10]) for s, a in PANEL.items()}
        rows = scan_assets(panel, NOW, CONFIG)
        self.assertTrue(all(r.rs.is_usable for r in rows))
        self.assertTrue(all(r.setup.confirmation != "CONFIRMED" for r in rows))
        self.assertTrue(all("stale_daily_structure" in r.market.quality_flags for r in rows))

    def test_missing_daily_degrades_only_that_asset(self):
        panel = {**PANEL, "SOL": replace(PANEL["SOL"], daily=[])}
        rows = scan_assets(panel, NOW, CONFIG)
        self.assertEqual(len(rows), 2)
        sol = next(r for r in rows if r.symbol == "SOL")
        self.assertIsNone(sol.market.active_cpr)
        self.assertEqual(sol.setup.label, "INSUFFICIENT_DATA")
        snapshot = scan_snapshot(NOW, rows, CONFIG)
        self.assertFalse(next(r for r in snapshot["rows"] if r["symbol"] == "SOL")["data_available"])

    def test_daily_gap_blocks_structure(self):
        asset = PANEL["SOL"]
        panel = {**PANEL, "SOL": replace(asset, daily=[*asset.daily[:-3], *asset.daily[-2:]])}
        row = next(r for r in scan_assets(panel, NOW, CONFIG) if r.symbol == "SOL")
        self.assertIn("daily_history_gap", row.market.quality_flags)

    def test_archive_error_cannot_be_hidden_by_fast_tick(self):
        scanner = self.scanner()
        with patch.object(scanner.research_archive, "record", side_effect=OSError("disk unavailable")) as record:
            self.assertFalse(scanner.tick(NOW, True))
            self.assertFalse(scanner.tick(NOW + timedelta(seconds=20), False))
        self.assertEqual(record.call_count, 2)
        self.assertEqual(scanner.status().candle_failures, 2)
        self.assertFalse((self.output / "scanner_latest.json").exists())

    def test_retry_commits_pending_close_after_transient_archive_error(self):
        scanner = self.scanner()
        with patch.object(scanner.research_archive, "record", side_effect=OSError("transient")):
            self.assertFalse(scanner.tick(NOW, True))
        self.assertTrue(scanner.tick(NOW + timedelta(seconds=20), False), scanner.status().last_error)
        self.assertEqual(scanner.research_archive.status().scans, 1)
        self.assertEqual(scanner.status().candle_failures, 0)
        self.assertTrue((self.output / "scanner_latest.json").exists())

    def test_failed_export_does_not_lose_or_duplicate_signal(self):
        scanner = self.scanner()
        snapshot = scan_snapshot(NOW, scan_assets(PANEL, NOW, CONFIG), CONFIG)
        with patch.object(scanner.signal_store, "export", side_effect=OSError("export failed")):
            with self.assertRaises(OSError):
                scanner._publish(snapshot)
        scanner._publish(snapshot)
        events = read_history(self.output / "signal_history.jsonl")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "activated")

    def test_failed_snapshot_write_and_restart_preserve_one_signal(self):
        scanner = self.scanner()
        snapshot = scan_snapshot(NOW, scan_assets(PANEL, NOW, CONFIG), CONFIG)
        with patch("terra_cpr.live.write_json_atomic", side_effect=OSError("snapshot failed")):
            with self.assertRaises(OSError):
                scanner._publish(snapshot)
        self.scanner()._publish(snapshot)
        self.assertEqual(len(read_history(self.output / "signal_history.jsonl")), 1)

    def test_missing_mid_is_reported_without_false_clearing(self):
        scanner = self.scanner()
        self.assertTrue(scanner.tick(NOW, True), scanner.status().last_error)
        with patch.object(scanner.source, "fetch_mids", return_value={s: a.price for s, a in PANEL.items() if s != "SOL"}):
            self.assertTrue(scanner.tick(NOW + timedelta(seconds=20), False))
        self.assertIn("SOL", scanner.status().excluded_symbols)
        self.assertEqual(scanner.status().selected_count, 3)
        self.assertEqual(scanner.status().price_available_count, 2)
        events = read_history(self.output / "signal_history.jsonl")
        self.assertFalse(any(e["event"] == "cleared" for e in events))
        self.assertTrue(scanner.tick(NOW + timedelta(seconds=40), False))
        self.assertEqual(len(read_history(self.output / "signal_history.jsonl")), 1)

    def test_valid_new_bar_can_clear_signal(self):
        store = SignalStore(self.output)
        snapshot = json.loads(json.dumps(scan_snapshot(NOW, scan_assets(PANEL, NOW, CONFIG), CONFIG), default=_json_default))
        store.record(snapshot)
        for row in snapshot["rows"]:
            row["input_cutoff"] = (NOW + timedelta(hours=1)).isoformat()
            row["setup"].update(label="NEUTRAL", direction="NONE", confirmation="NONE")
        events = store.record(snapshot)
        self.assertEqual([e["event"] for e in events], ["cleared"])

    def test_archive_preserves_actual_observation_and_complete_config(self):
        path = self.output / "archive.sqlite3"
        ResearchArchive(path).record(PANEL, factor_rows(), CONFIG, observed_at=NOW + timedelta(minutes=59))
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT as_of,observed_at,config_json,config_hash,source_hash FROM scans").fetchone()
        self.assertEqual(row[0], NOW.isoformat())
        self.assertEqual(row[1], (NOW + timedelta(minutes=59)).isoformat())
        self.assertEqual(json.loads(row[2]), asdict(CONFIG))
        self.assertEqual(len(row[3]), 64)
        self.assertEqual(len(row[4]), 64)

    def test_archive_replay_preserves_pre_correction_inputs(self):
        path = self.output / "archive.sqlite3"
        archive = ResearchArchive(path)
        archive.record(PANEL, factor_rows(), CONFIG)
        panel = advance()
        asset = panel["SOL"]
        bar = asset.intraday[-3]
        corrected = replace(bar, close=bar.close * 1.00001, high=bar.high * 1.00001)
        panel["SOL"] = replace(asset, intraday=[*asset.intraday[:-3], corrected, *asset.intraday[-2:]])
        archive.record(panel, factor_rows(panel, NOW + timedelta(hours=1)), CONFIG)
        rebuilt = list(archived_panels(path))
        self.assertEqual(rebuilt[0][2]["SOL"].intraday, PANEL["SOL"].intraday)
        self.assertEqual(rebuilt[1][2]["SOL"].intraday, panel["SOL"].intraday)
        _, audit, *_ = replay_archive(path, [4])
        self.assertEqual(audit["mismatches"], [])

    def test_configuration_change_requires_new_experiment_archive(self):
        archive = ResearchArchive(self.output / "archive.sqlite3")
        archive.record(PANEL, factor_rows(), CONFIG)
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            archive.record(advance(), factor_rows(), replace(CONFIG, candidate_persistence=0.5))

    def test_v1_archive_migration_preserves_history_and_unknown_provenance(self):
        path = self.output / "archive.sqlite3"
        before = ResearchArchive(path).record(PANEL, factor_rows(), CONFIG)
        # Strip the additive v2 fields to reproduce the shipped v1 schema.
        with sqlite3.connect(path) as db:
            for table, columns in {
                "scans": ("observed_at", "evaluated_at", "config_json", "config_hash", "source_hash", "code_revision", "selection_json"),
                "panel_rows": ("observed_at", "input_json", "factor_diagnostics_json"),
                "research_evaluations": ("unavailable_rows",),
            }.items():
                for column in columns:
                    db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            db.execute("DROP TABLE candle_versions")
            db.execute("UPDATE archive_metadata SET value='1' WHERE key='schema_version'")
        migrated = ResearchArchive(path).status()
        self.assertEqual(migrated.scans, before.scans)
        self.assertEqual(migrated.events, before.events)
        self.assertEqual(migrated.candles, before.candles)
        self.assertEqual(migrated.schema_version, 2)
        self.assertEqual(ResearchArchive(path).status().events, before.events)
        with self.assertRaisesRegex(ValueError, "predates input provenance"):
            list(archived_panels(path))

    def test_replay_command_verifies_actual_recorded_panels(self):
        path = self.output / "archive.sqlite3"
        archive = ResearchArchive(path)
        panel = PANEL
        for hour in range(6):
            if hour:
                panel = advance(panel)
            when = NOW + timedelta(hours=hour)
            archive.record(panel, factor_rows(panel, when), CONFIG, observed_at=when + timedelta(seconds=30))
        target = self.output / "replay.json"
        write_replay(SimpleNamespace(archive=path, output=target, funding_series=None,
            horizon_bars=[1], train_fraction=0.65, holdout_fraction=0.2, folds=3,
            include_holdout=False, cost_bps=10))
        report = json.loads(target.read_text())
        self.assertEqual(report["audit"]["scans"], 6)
        self.assertFalse(report["audit"]["mismatches"])
        self.assertGreater(report["audit"]["verified_observations"], 0)
        self.assertEqual(len(report["results"]), 5)

    def test_future_context_cannot_be_joined_to_an_earlier_event(self):
        context = MarketContext(observed_at=NOW + timedelta(days=1), funding_rate=0.5)
        with self.assertRaisesRegex(ValueError, "observed_at"):
            generate_point_in_time_events(PANEL, CONFIG, 4, context_at=lambda *_: context)

    def test_missing_secondary_factor_is_visible(self):
        rows = scan_assets({s: a for s, a in PANEL.items() if s != "ETH"}, NOW, CONFIG)
        self.assertEqual(len(rows), 1)
        self.assertIn("secondary_factor_unavailable", rows[0].rs.quality_flags)

    def test_pending_refresh_keeps_scheduler_due_after_failure(self):
        scanner = self.scanner()
        class TwoTicks:
            count = 0
            def is_set(self):
                return self.count >= 2
            def wait(self, _delay):
                self.count += 1
        scanner._stop = TwoTicks()
        with patch.object(scanner.research_archive, "record", side_effect=OSError("persistent")):
            with patch("terra_cpr.live.datetime", SimpleNamespace(now=lambda _: NOW, fromtimestamp=datetime.fromtimestamp, fromisoformat=datetime.fromisoformat)):
                scanner._run()
        self.assertIsNone(scanner.status().next_candle_refresh)
        self.assertEqual(scanner.status().candle_failures, 2)

    def test_archive_valid_inactive_state_clears_but_unavailable_preserves(self):
        archive = ResearchArchive(self.output / "archive.sqlite3")
        first = archive.record(PANEL, factor_rows(), CONFIG)
        unavailable = {m: [replace(row, rs=replace(row.rs, quality_flags=("stale_or_misaligned",))) for row in rows] for m, rows in factor_rows().items()}
        panel = advance()
        status = archive.record(panel, unavailable, CONFIG)
        self.assertEqual(status.active_states, first.active_states)
        panel = advance(panel)
        inactive = {m: [replace(row, rs=replace(row.rs, score=0, discovery_score=0, horizon_excess_return={"medium": 0}), setup=replace(row.setup, label="NEUTRAL", confirmation="NONE")) for row in rows] for m, rows in factor_rows(panel, NOW + timedelta(hours=2)).items()}
        status = archive.record(panel, inactive, CONFIG)
        self.assertEqual(status.active_states, 0)

    def test_same_timestamp_never_straddles_split(self):
        events = [EventOutcome(NOW, s, "LONG", 0.01, horizon_bars=24) for s in ("A", "B", "C", "D")]
        train, test = chronological_split(events, 0.5)
        self.assertFalse(set(e.timestamp for e in train) & set(e.timestamp for e in test))

    def test_training_outcomes_are_purged_at_boundary(self):
        events = [EventOutcome(NOW + timedelta(hours=i), "A", "LONG", 0.01, horizon_bars=24) for i in range(80)]
        boundary = NOW + timedelta(hours=40)
        train, test = chronological_split(events, boundary=boundary)
        self.assertTrue(all(e.timestamp + timedelta(hours=24) < boundary for e in train))
        self.assertTrue(all(e.timestamp >= boundary for e in test))
        self.assertEqual(len(train), 16)

    def test_holdout_is_not_exposed_until_explicitly_released(self):
        events = [EventOutcome(NOW + timedelta(days=i), "A", "LONG", 0.01, exit_timestamp=NOW + timedelta(days=i, hours=1)) for i in range(100)]
        plan = calendar_plan(NOW, NOW + timedelta(days=100))
        result = summarize_events(events, plan, STEP, 10)
        self.assertFalse(result["holdout"]["released"])
        self.assertNotIn("holdout", result["asset_return"])
        self.assertTrue(all(e["timestamp"] < plan["holdout_start"] for e in result["events"]))
        released = summarize_events(events, plan, STEP, 10, True)
        self.assertEqual(released["asset_return"]["holdout"]["count"], 20)

    def test_overlapping_events_have_no_fictitious_portfolio_metrics(self):
        metrics = evaluate([EventOutcome(NOW, s, "LONG", 0.1) for s in ("A", "B")], 0)
        self.assertIsNone(metrics.total_return)
        self.assertIsNone(metrics.max_drawdown)
        self.assertIsNone(metrics.sharpe)
        self.assertEqual(metrics.expectancy, 0.1)
        self.assertEqual(metrics.timestamp_count, 1)

    def test_time_block_uncertainty_is_reproducible(self):
        events = [EventOutcome(NOW + timedelta(days=i), s, "LONG", (-1)**i * 0.01, exit_timestamp=NOW + timedelta(days=i+1)) for i in range(15) for s in ("A", "B")]
        first = evaluate(events, 0)
        self.assertIsNotNone(first.block_bootstrap_ci95)
        self.assertEqual(first, evaluate(events, 0))
        self.assertEqual(first.timestamp_count, 15)

    def test_missing_funding_is_not_zero(self):
        path = self.output / "funding.json"
        raw = {"interval_seconds": STEP, "coverage_start": NOW.isoformat(), "coverage_end": (NOW + timedelta(hours=3)).isoformat(), "rates": {"SOL": [{"timestamp": (NOW + timedelta(hours=1)).isoformat(), "rate": 0.001}]}}
        path.write_text(json.dumps(raw))
        funding = load_funding_series(path)
        self.assertEqual(funding("SOL", NOW, NOW + timedelta(hours=1), "LONG"), 0.001)
        self.assertEqual(funding("SOL", NOW, NOW + timedelta(hours=1), "SHORT"), -0.001)
        self.assertIsNone(funding("SOL", NOW, NOW + timedelta(hours=2), "LONG"))

    def test_invalid_live_and_engine_configuration_fails_early(self):
        for values in ({"fast_interval_seconds": 0}, {"fast_interval_seconds": float("nan")}, {"settle_seconds": -1}, {"hourly_window": 3.1}, {"universe_size": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                LiveConfig(**values)
        for values in ({"beta_window": 720.5}, {"winsor_mad": float("inf")}, {"min_alignment_ratio": True}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RSConfig(**values)

    def test_unknown_configuration_section_is_rejected(self):
        path = self.output / "config.toml"
        path.write_text("[relative_strenght]\nbeta_window=100\n")
        with self.assertRaisesRegex(ValueError, "unknown configuration section"):
            load_live_configs(path)

    def test_fractional_bar_interval_cannot_be_truncated(self):
        path = self.output / "config.toml"
        path.write_text("[scanner]\ninterval_seconds=3600.5\n")
        with self.assertRaises(ValueError):
            load_scanner_config(path, 3600)

    def test_fallback_toml_rejects_duplicate_keys_and_preserves_hash(self):
        with patch("terra_cpr.config.tomllib", None):
            self.assertEqual(_read_toml('[scanner]\nbenchmark="A#B" # comment')["scanner"]["benchmark"], "A#B")
            with self.assertRaises(ValueError):
                _read_toml("[scanner]\ninterval_seconds=3600\ninterval_seconds=900")

    def test_shutdown_interrupts_retry_backoff(self):
        scanner = self.scanner()
        scanner._stop.set()
        with self.assertRaises(InterruptedError):
            scanner._interruptible_wait(30)

    def test_future_prices_do_not_change_earlier_triggers(self):
        config = ScannerConfig(rs=RSConfig(beta_window=60, min_beta_points=20, persistence_window=8,
                                           short_horizon_bars=4, medium_horizon_bars=8, long_horizon_bars=14, min_empirical_windows=5))
        panel = {s: replace(a, intraday=a.intraday[-120:]) for s, a in PANEL.items()}
        cutoff = NOW - timedelta(hours=40)
        altered = {s: replace(a, intraday=[replace(c, open=c.open*1.2, high=c.high*1.2, low=c.low*1.2, close=c.close*1.2) if c.timestamp >= cutoff else c for c in a.intraday]) for s, a in panel.items()}
        def triggers(inputs):
            events = generate_point_in_time_events(inputs, config, 4, event_rule="strong_discovery")
            return [(e.timestamp, e.symbol, e.score, e.features) for e in events if e.timestamp <= cutoff]
        before = triggers(panel)
        self.assertTrue(before)
        self.assertEqual(before, triggers(altered))


if __name__ == "__main__":
    unittest.main()
