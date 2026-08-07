"""Tests for the live Hyperliquid panel loop. No test performs real network I/O."""
from __future__ import annotations

import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone

from terra_cpr.models import Candle
from terra_cpr.live import (
    CandleCache,
    LoadShedError,
    ThrottledCandleFetcher,
    UniverseError,
    build_panel,
    select_universe,
)

HOUR = 3600
BASE = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)


def _bar(index: int, close: float = 100.0, interval: int = HOUR) -> Candle:
    return Candle(
        timestamp=BASE + timedelta(seconds=index * interval),
        open=close, high=close * 1.01, low=close * 0.99, close=close, volume=1.0,
    )


def _meta(*names: str) -> dict:
    return {"universe": [{"name": name, "szDecimals": 2, "maxLeverage": 20} for name in names]}


def _ctxs(*volumes: object) -> list[dict]:
    return [{"dayNtlVlm": str(volume), "markPx": "1.0"} for volume in volumes]


class SelectUniverseTests(unittest.TestCase):
    def test_ranks_by_24h_notional_volume(self):
        symbols = select_universe(_meta("BTC", "AAA", "BBB"), _ctxs(900, 100, 500), size=3)
        self.assertEqual(symbols, ["BTC", "BBB", "AAA"])

    def test_excludes_delisted_markets(self):
        meta = _meta("BTC", "DEAD", "AAA")
        meta["universe"][1]["isDelisted"] = True
        symbols = select_universe(meta, _ctxs(900, 800, 100), size=3)
        self.assertEqual(symbols, ["BTC", "AAA"])

    def test_truncates_to_requested_size(self):
        symbols = select_universe(_meta("BTC", "AAA", "BBB", "CCC"), _ctxs(900, 10, 500, 300), size=3)
        self.assertEqual(symbols, ["BTC", "BBB", "CCC"])

    def test_benchmark_is_retained_even_when_volume_rank_is_low(self):
        symbols = select_universe(_meta("BTC", "AAA", "BBB"), _ctxs(1, 900, 500), size=2)
        self.assertIn("BTC", symbols)
        self.assertEqual(len(symbols), 2)

    def test_missing_benchmark_is_an_error(self):
        with self.assertRaises(UniverseError):
            select_universe(_meta("AAA", "BBB"), _ctxs(900, 500), size=2)

    def test_renamed_volume_field_fails_loudly(self):
        with self.assertRaises(UniverseError):
            select_universe(_meta("BTC", "AAA"), [{"volume24h": "5"}, {"volume24h": "5"}], size=2)

    def test_length_mismatch_between_universe_and_contexts_is_an_error(self):
        with self.assertRaises(UniverseError):
            select_universe(_meta("BTC", "AAA", "BBB"), _ctxs(900, 500), size=3)

    def test_unparseable_volume_fails_loudly(self):
        with self.assertRaises(UniverseError):
            select_universe(_meta("BTC", "AAA"), _ctxs(900, "not-a-number"), size=2)


class CandleCacheTests(unittest.TestCase):
    def test_stores_bars_in_ascending_timestamp_order(self):
        cache = CandleCache(window=10)
        cache.merge("BTC", [_bar(2), _bar(0), _bar(1)])
        self.assertEqual([c.timestamp for c in cache.get("BTC")], [_bar(0).timestamp, _bar(1).timestamp, _bar(2).timestamp])

    def test_a_later_merge_replaces_a_bar_with_the_same_open(self):
        cache = CandleCache(window=10)
        cache.merge("BTC", [_bar(0, close=100.0)])
        cache.merge("BTC", [_bar(0, close=250.0)])
        bars = cache.get("BTC")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].close, 250.0)

    def test_evicts_bars_beyond_the_window(self):
        cache = CandleCache(window=3)
        cache.merge("BTC", [_bar(index) for index in range(6)])
        bars = cache.get("BTC")
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0].timestamp, _bar(3).timestamp)

    def test_a_gap_is_preserved_and_never_filled(self):
        cache = CandleCache(window=10)
        cache.merge("BTC", [_bar(0), _bar(3)])
        self.assertEqual([c.timestamp for c in cache.get("BTC")], [_bar(0).timestamp, _bar(3).timestamp])

    def test_last_timestamp_reports_the_newest_bar_open(self):
        cache = CandleCache(window=10)
        self.assertIsNone(cache.last_timestamp("BTC"))
        cache.merge("BTC", [_bar(0), _bar(4)])
        self.assertEqual(cache.last_timestamp("BTC"), _bar(4).timestamp)

    def test_symbols_do_not_share_storage(self):
        cache = CandleCache(window=10)
        cache.merge("BTC", [_bar(0)])
        cache.merge("ETH", [_bar(1)])
        self.assertEqual(len(cache.get("BTC")), 1)
        self.assertEqual(cache.get("ETH")[0].timestamp, _bar(1).timestamp)

    def test_unknown_symbol_returns_no_bars(self):
        self.assertEqual(CandleCache(window=10).get("NOPE"), [])


def _day(index: int, open_price: float = 100.0) -> Candle:
    return Candle(
        timestamp=BASE + timedelta(days=index), open=open_price,
        high=open_price * 1.05, low=open_price * 0.95, close=open_price * 1.02, volume=1.0,
    )


class BuildPanelTests(unittest.TestCase):
    """The forming daily bar supplies session_open and nothing else."""

    def setUp(self):
        self.hourly = CandleCache(window=100)
        # Three complete days plus a fourth day that is still forming at as_of.
        self.daily = {"BTC": [_day(0), _day(1), _day(2), _day(3, open_price=555.0)]}
        self.as_of = BASE + timedelta(days=3, hours=6)
        for index in range(8):
            self.hourly.merge("BTC", [_bar(index)])

    def _panel(self, **kwargs):
        params = dict(
            symbols=["BTC"], hourly=self.hourly, daily=self.daily,
            mids={"BTC": 123.0}, prior_prices={}, as_of=self.as_of,
            interval_seconds=HOUR, benchmark="BTC",
        )
        params.update(kwargs)
        return build_panel(**params)

    def test_session_open_comes_from_the_forming_daily_bar(self):
        self.assertEqual(self._panel()["BTC"].session_open, 555.0)

    def test_the_forming_daily_bar_never_reaches_the_engine(self):
        daily = self._panel()["BTC"].daily
        self.assertEqual(len(daily), 3)
        self.assertEqual(daily[-1].timestamp, _day(2).timestamp)

    def test_session_open_is_none_when_no_day_is_forming(self):
        panel = self._panel(daily={"BTC": [_day(0), _day(1), _day(2)]})
        self.assertIsNone(panel["BTC"].session_open)

    def test_price_comes_from_the_current_mid(self):
        self.assertEqual(self._panel()["BTC"].price, 123.0)

    def test_prior_price_is_carried_from_the_previous_tick(self):
        self.assertEqual(self._panel(prior_prices={"BTC": 99.0})["BTC"].prior_price, 99.0)

    def test_prior_price_is_absent_on_the_first_tick(self):
        self.assertIsNone(self._panel()["BTC"].prior_price)

    def test_forming_hourly_bar_is_excluded(self):
        forming = Candle(
            timestamp=self.as_of, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
        )
        self.hourly.merge("BTC", [forming])
        self.assertNotIn(forming.timestamp, [c.timestamp for c in self._panel()["BTC"].intraday])

    def test_symbol_without_a_mid_is_omitted(self):
        panel = self._panel(symbols=["BTC", "GHOST"], daily={**self.daily, "GHOST": [_day(0), _day(1)]})
        self.assertNotIn("GHOST", panel)

    def test_missing_benchmark_mid_is_an_error(self):
        with self.assertRaises(UniverseError):
            self._panel(mids={})


class _FakeClock:
    """Deterministic monotonic clock; sleeping advances it rather than waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class _ScriptedSource:
    """Returns each queued response in turn; records every call."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple] = []

    def fetch_candles(self, symbol, interval, start, end):
        self.calls.append((symbol, interval, start, end))
        if not self.responses:
            raise AssertionError("source called more times than the test scripted")
        return self.responses.pop(0)


class ThrottledCandleFetcherTests(unittest.TestCase):
    def _fetcher(self, responses, **kwargs):
        clock = _FakeClock()
        source = _ScriptedSource(responses)
        fetcher = ThrottledCandleFetcher(
            source=source, min_gap_seconds=0.1, max_attempts=3,
            sleep=clock.sleep, monotonic=clock.monotonic, **kwargs,
        )
        return fetcher, source, clock

    def test_returns_bars_from_the_source(self):
        fetcher, source, _ = self._fetcher([[_bar(0), _bar(1)]])
        bars = fetcher.fetch("BTC", "1h", BASE, BASE + timedelta(hours=2))
        self.assertEqual(len(bars), 2)
        self.assertEqual(len(source.calls), 1)

    def test_empty_response_is_retried_rather_than_read_as_no_data(self):
        """Hyperliquid sheds load with HTTP 200 and an empty array, not a 429."""
        fetcher, source, _ = self._fetcher([[], [_bar(0)]])
        bars = fetcher.fetch("BTC", "1h", BASE, BASE + timedelta(hours=1))
        self.assertEqual(len(bars), 1)
        self.assertEqual(len(source.calls), 2)

    def test_persistent_emptiness_raises_rather_than_returning_nothing(self):
        fetcher, source, _ = self._fetcher([[], [], []])
        with self.assertRaises(LoadShedError):
            fetcher.fetch("BTC", "1h", BASE, BASE + timedelta(hours=1))
        self.assertEqual(len(source.calls), 3)

    def test_retry_backoff_grows(self):
        fetcher, _, clock = self._fetcher([[], [], [_bar(0)]])
        fetcher.fetch("BTC", "1h", BASE, BASE + timedelta(hours=1))
        backoffs = [s for s in clock.slept if s >= 0.2]
        self.assertEqual(len(backoffs), 2)
        self.assertGreater(backoffs[1], backoffs[0])

    def test_throttle_sleeps_between_back_to_back_requests(self):
        fetcher, _, clock = self._fetcher([[_bar(0)], [_bar(1)]])
        fetcher.fetch("AAA", "1h", BASE, BASE + timedelta(hours=1))
        fetcher.fetch("BBB", "1h", BASE, BASE + timedelta(hours=1))
        self.assertTrue(any(abs(s - 0.1) < 1e-9 for s in clock.slept))

    def test_no_throttle_sleep_when_the_gap_has_already_elapsed(self):
        fetcher, _, clock = self._fetcher([[_bar(0)], [_bar(1)]])
        fetcher.fetch("AAA", "1h", BASE, BASE + timedelta(hours=1))
        clock.now += 5.0
        clock.slept.clear()
        fetcher.fetch("BBB", "1h", BASE, BASE + timedelta(hours=1))
        self.assertEqual(clock.slept, [])


class _FakeSource:
    """Stands in for the public data adapter; records every request range."""

    def __init__(self, symbols, mids=None, volumes=None):
        self.symbols = list(symbols)
        self.mids = mids or {name: 100.0 + index for index, name in enumerate(self.symbols)}
        self.volumes = volumes or {name: 1000.0 - index for index, name in enumerate(self.symbols)}
        self.calls: list[tuple] = []
        self.meta_calls = 0
        self.fail_with: Exception | None = None

    def fetch_meta_and_contexts(self):
        self.meta_calls += 1
        if self.fail_with:
            raise self.fail_with
        meta = {"universe": [{"name": name, "szDecimals": 2} for name in self.symbols]}
        ctxs = [{"dayNtlVlm": str(self.volumes[name])} for name in self.symbols]
        return meta, ctxs

    def fetch_mids(self):
        if self.fail_with:
            raise self.fail_with
        return dict(self.mids)

    def fetch_candles(self, symbol, interval, start, end):
        """Synthesise a grid-aligned bar series covering whatever range is asked for."""
        self.calls.append((symbol, interval, start, end))
        if self.fail_with:
            raise self.fail_with
        step = timedelta(days=1) if interval == "1d" else timedelta(hours=1)
        cursor = start.replace(minute=0, second=0, microsecond=0)
        if interval == "1d":
            cursor = cursor.replace(hour=0)
        bars, price = [], 100.0
        while cursor <= end:
            bars.append(Candle(cursor, price, price * 1.02, price * 0.98, price * 1.01, 1.0))
            cursor += step
            price += 0.5
        return bars


class LiveScannerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from terra_cpr.live import LiveConfig, LiveScanner

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.as_of = BASE + timedelta(days=3, hours=6)
        self.source = _FakeSource(["BTC", "AAA", "BBB"])
        self.clock = _FakeClock()
        self.scanner = LiveScanner(
            output_dir=self.output, source=self.source,
            live_config=LiveConfig(universe_size=3, hourly_window=50, daily_window=10),
            sleep=self.clock.sleep, monotonic=self.clock.monotonic,
        )

    def _snapshot(self):
        import json
        return json.loads((self.output / "scanner_latest.json").read_text())

    def test_tick_writes_a_snapshot_with_a_row_per_non_benchmark_asset(self):
        self.scanner.tick(self.as_of, refresh_candles=True)
        snapshot = self._snapshot()
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual({row["symbol"] for row in snapshot["rows"]}, {"AAA", "BBB"})

    def test_snapshot_records_the_thresholds_that_produced_its_labels(self):
        """A reader must not have to guess which rule a label came from."""
        from terra_cpr.scanner import ScannerConfig
        from terra_cpr.live import LiveConfig, LiveScanner

        scanner = LiveScanner(
            output_dir=self.output, source=self.source,
            scanner_config=ScannerConfig(candidate_rs_score=6.0, candidate_persistence=0.7),
            live_config=LiveConfig(universe_size=3, hourly_window=50, daily_window=10),
            sleep=self.clock.sleep, monotonic=self.clock.monotonic,
        )
        scanner.tick(self.as_of, refresh_candles=True)
        self.assertEqual(
            self._snapshot()["gates"],
            {"candidate_rs_score": 6.0, "candidate_persistence": 0.7},
        )

    def test_cold_start_requests_a_wide_window_then_only_the_delta(self):
        self.scanner.tick(self.as_of, refresh_candles=True)
        cold = [c for c in self.source.calls if c[0] == "AAA" and c[1] == "1h"][0]
        self.source.calls.clear()
        self.scanner.tick(self.as_of + timedelta(hours=1), refresh_candles=True)
        warm = [c for c in self.source.calls if c[0] == "AAA" and c[1] == "1h"][0]
        self.assertGreater(warm[2], cold[2], "warm top-up must start later than the cold fetch")

    def test_fast_tick_does_not_touch_the_candle_endpoint(self):
        self.scanner.tick(self.as_of, refresh_candles=True)
        self.source.calls.clear()
        self.scanner.tick(self.as_of, refresh_candles=False)
        self.assertEqual(self.source.calls, [])

    def test_each_tick_captures_its_prices_for_the_next_tick(self):
        """Paired with BuildPanelTests, this is what makes fresh R1/S1 crosses detectable."""
        self.assertEqual(self.scanner.prior_prices, {})
        self.scanner.tick(self.as_of, refresh_candles=True)
        first = self.source.mids["AAA"]
        self.assertEqual(self.scanner.prior_prices["AAA"], first)
        self.source.mids["AAA"] = first * 1.5
        self.scanner.tick(self.as_of, refresh_candles=False)
        self.assertEqual(self.scanner.prior_prices["AAA"], first * 1.5)

    def test_a_failing_tick_records_the_error_and_keeps_the_previous_snapshot(self):
        self.scanner.tick(self.as_of, refresh_candles=True)
        original = self._snapshot()
        self.source.fail_with = RuntimeError("endpoint down")
        self.scanner.tick(self.as_of + timedelta(hours=1), refresh_candles=True)
        status = self.scanner.status()
        self.assertEqual(status.consecutive_failures, 1)
        self.assertIn("endpoint down", status.last_error or "")
        self.assertEqual(self._snapshot(), original)

    def test_a_successful_tick_clears_the_failure_counter(self):
        self.source.fail_with = RuntimeError("transient")
        self.scanner.tick(self.as_of, refresh_candles=True)
        self.assertEqual(self.scanner.status().consecutive_failures, 1)
        self.source.fail_with = None
        self.scanner.tick(self.as_of, refresh_candles=True)
        self.assertEqual(self.scanner.status().consecutive_failures, 0)
        self.assertIsNone(self.scanner.status().last_error)

    def test_status_reports_the_universe_size_after_a_tick(self):
        self.scanner.tick(self.as_of, refresh_candles=True)
        self.assertEqual(self.scanner.status().universe_size, 3)

    def test_thread_stops_promptly_when_asked(self):
        thread = self.scanner.start()
        try:
            self.assertTrue(thread.is_alive())
        finally:
            self.scanner.stop(timeout=5.0)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
