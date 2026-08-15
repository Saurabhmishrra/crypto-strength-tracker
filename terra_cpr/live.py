"""Live public-market panel construction for the Terra CPR scanner.

This module reads public Hyperliquid market data and assembles the same
``AssetInput`` panel the fixture adapter produces. It holds no credentials, no
signing code, and no order path, and it never mutates a scanner engine.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .data import completed_bars
from .models import Candle
from .report import scan_snapshot, write_json_atomic
from .scanner import AssetInput, ScannerConfig, scan_assets
from .signal_history import append_history, signal_transitions

DAY_SECONDS = 86_400

#: Two completed sessions is the hard floor for a CPR: yesterday defines today's
#: levels and the one before it is needed to place them in context. Below this,
#: ``build_market_structure`` raises rather than guessing.
MIN_DAILY_BARS = 2


class UniverseError(RuntimeError):
    """Raised when the public metadata response is not the shape we require.

    Universe selection fails loudly rather than falling back to an arbitrary
    slice: a silently truncated universe corrupts every cross-sectional rank
    that depends on it.
    """


def select_universe(
    meta: Mapping[str, Any],
    contexts: Sequence[Mapping[str, Any]],
    size: int,
    benchmark: str = "BTC",
) -> list[str]:
    """Rank tradable perps by 24h notional volume, benchmark always retained.

    ``meta['universe']`` and ``contexts`` are positionally aligned lists in the
    public ``metaAndAssetCtxs`` response; that index correspondence is the only
    join available, so a length mismatch is unrecoverable rather than partial.
    """
    if size <= 0:
        raise UniverseError("universe size must be positive")
    entries = meta.get("universe")
    if not isinstance(entries, list):
        raise UniverseError("metadata is missing a 'universe' list")
    if len(entries) != len(contexts):
        raise UniverseError(
            f"universe/context length mismatch: {len(entries)} vs {len(contexts)}"
        )

    ranked: list[tuple[float, str]] = []
    for entry, context in zip(entries, contexts):
        name = entry.get("name")
        if not isinstance(name, str):
            raise UniverseError("a universe entry is missing its 'name'")
        if entry.get("isDelisted"):
            continue
        if "dayNtlVlm" not in context:
            raise UniverseError(f"context for {name} is missing 'dayNtlVlm'")
        try:
            volume = float(context["dayNtlVlm"])
        except (TypeError, ValueError) as exc:
            raise UniverseError(f"unparseable 'dayNtlVlm' for {name}: {exc}") from exc
        ranked.append((volume, name))

    if not any(name == benchmark for _, name in ranked):
        raise UniverseError(f"benchmark {benchmark} is absent from the tradable universe")

    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = ranked[:size]
    if not any(name == benchmark for _, name in selected):
        benchmark_entry = next(item for item in ranked if item[1] == benchmark)
        selected = selected[:-1] + [benchmark_entry]
        selected.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in selected]


class CandleCache:
    """Rolling per-symbol bar store keyed by bar-open timestamp.

    Refresh fetches deliberately overlap the stored tail so a bar captured while
    it was still forming is replaced by its completed version. Gaps are left as
    gaps: forward-filling would manufacture the very returns the relative-strength
    engine measures, so a hole must survive to be reported as reduced history.
    """

    def __init__(self, window: int) -> None:
        if window <= 0:
            raise ValueError("cache window must be positive")
        self.window = window
        self._bars: dict[str, dict[datetime, Candle]] = {}

    def merge(self, symbol: str, candles: Sequence[Candle]) -> None:
        stored = self._bars.setdefault(symbol, {})
        for candle in candles:
            stored[candle.timestamp] = candle
        if len(stored) > self.window:
            for timestamp in sorted(stored)[: len(stored) - self.window]:
                del stored[timestamp]

    def get(self, symbol: str) -> list[Candle]:
        stored = self._bars.get(symbol)
        return [stored[timestamp] for timestamp in sorted(stored)] if stored else []

    def last_timestamp(self, symbol: str) -> Optional[datetime]:
        stored = self._bars.get(symbol)
        return max(stored) if stored else None


class LoadShedError(RuntimeError):
    """Raised when a candle request kept coming back empty.

    The public ``/info`` endpoint sheds load by answering HTTP 200 with an empty
    array rather than a 429. For a symbol that is in the universe — and therefore
    has traded in the last 24 hours — an empty window means "ask again", never
    "this market has no data". Reading it as the latter would quietly shrink the
    panel and corrupt every cross-sectional rank computed from it.
    """


@dataclass
class ThrottledCandleFetcher:
    """Rate-limited candle reader that retries the empty-array shed response."""

    source: Any
    min_gap_seconds: float = 0.12
    max_attempts: int = 4
    base_backoff_seconds: float = 0.4
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    _last_request: Optional[float] = field(default=None, repr=False)

    def _throttle(self) -> None:
        if self._last_request is not None:
            remaining = self.min_gap_seconds - (self.monotonic() - self._last_request)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request = self.monotonic()

    def fetch(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Candle]:
        for attempt in range(self.max_attempts):
            self._throttle()
            candles = self.source.fetch_candles(symbol, interval, start, end)
            if candles:
                return list(candles)
            if attempt < self.max_attempts - 1:
                self.sleep(self.base_backoff_seconds * (2 ** attempt))
        raise LoadShedError(
            f"{symbol} {interval} returned an empty candle window "
            f"{self.max_attempts} times; treating as load shedding, not as absent data"
        )


def _session_open(daily_bars: Sequence[Candle], as_of: datetime) -> Optional[float]:
    """Read the current session's open from the *forming* daily bar.

    This is the one place an incomplete candle is read, and only for this single
    field. The forming bar never reaches ``build_market_structure``: yesterday's
    completed session is what defines today's CPR and pivots.
    """
    forming = [
        candle for candle in daily_bars
        if candle.timestamp <= as_of < candle.timestamp + timedelta(seconds=DAY_SECONDS)
    ]
    return forming[-1].open if forming else None


def build_panel(
    symbols: Sequence[str],
    hourly: CandleCache,
    daily: Mapping[str, Sequence[Candle]],
    mids: Mapping[str, float],
    prior_prices: Mapping[str, float],
    as_of: datetime,
    interval_seconds: int,
    benchmark: str = "BTC",
) -> dict[str, AssetInput]:
    """Assemble the scanner panel from cached bars and current mid prices.

    Every bar handed to an engine is filtered through ``completed_bars``; the
    only value taken from an unfinished candle is ``session_open``.
    """
    if benchmark not in mids:
        raise UniverseError(f"no mid price for benchmark {benchmark}; cannot scan")
    panel: dict[str, AssetInput] = {}
    for symbol in symbols:
        price = mids.get(symbol)
        if price is None:
            continue
        daily_bars = list(daily.get(symbol, ()))
        panel[symbol] = AssetInput(
            symbol=symbol,
            price=float(price),
            daily=completed_bars(daily_bars, DAY_SECONDS, as_of),
            intraday=completed_bars(hourly.get(symbol), interval_seconds, as_of),
            session_open=_session_open(daily_bars, as_of),
            prior_price=prior_prices.get(symbol),
        )
    return panel


@dataclass(frozen=True)
class LiveConfig:
    """Cadence and depth settings for the live loop."""

    universe_size: int = 60
    fast_interval_seconds: float = 20.0
    settle_seconds: float = 30.0
    hourly_window: int = 720
    daily_window: int = 140
    min_gap_seconds: float = 0.12


@dataclass(frozen=True)
class LiveStatus:
    """What the health rail needs to tell the truth about the feed."""

    running: bool
    universe_size: int
    last_tick: Optional[str]
    last_candle_refresh: Optional[str]
    next_candle_refresh: Optional[str]
    consecutive_failures: int
    last_error: Optional[str]
    #: Symbols the universe selected but the caches cannot support yet, mapped
    #: to why. Never empty silently: a shrunken panel has to be visible.
    excluded_symbols: Mapping[str, str] = field(default_factory=dict)


class LiveScanner:
    """Two-tier public-data loop writing scanner snapshots.

    The fast tier refreshes mid prices only, because the structure leg of the
    decision rule moves continuously while relative strength cannot change until
    an hourly bar closes. The slow tier tops candles up just after each close.
    Both tiers call the same ``scan_assets``, so the tiers cannot disagree about
    strategy logic — only about how fresh their inputs are.
    """

    def __init__(
        self,
        output_dir: Path,
        source: Any,
        scanner_config: ScannerConfig = ScannerConfig(),
        live_config: LiveConfig = LiveConfig(),
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.source = source
        self.scanner_config = scanner_config
        self.live_config = live_config
        self.fetcher = ThrottledCandleFetcher(
            source=source, min_gap_seconds=live_config.min_gap_seconds,
            sleep=sleep, monotonic=monotonic,
        )
        self.hourly = CandleCache(window=live_config.hourly_window)
        self.daily = CandleCache(window=live_config.daily_window)
        self.prior_prices: dict[str, float] = {}
        self.symbols: list[str] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_tick: Optional[datetime] = None
        self._last_candle_refresh: Optional[datetime] = None
        self._next_candle_refresh: Optional[datetime] = None
        self._failures = 0
        self._error: Optional[str] = None
        self._excluded: dict[str, str] = {}

    # -- data acquisition -------------------------------------------------

    def _refresh_universe(self) -> list[str]:
        """Select the candidate universe. Candidates are not yet scannable."""
        meta, contexts = self.source.fetch_meta_and_contexts()
        return select_universe(
            meta, contexts, self.live_config.universe_size, self.scanner_config.benchmark
        )

    def _refresh_candles(self, candidates: Sequence[str], as_of: datetime) -> list[str]:
        """Top candles up, then admit only the symbols the caches can support.

        Admission is what keeps a rotating universe honest. A symbol becomes
        scannable when its cache holds the two completed daily bars every CPR
        needs -- not when it wins a volume rank. Publishing the candidate list
        before the bars arrive is what once wedged this loop: ``scan_assets``
        has no per-asset guard, so a single symbol without daily bars raised
        through the whole panel, and fast ticks never refetch, so it could not
        heal until the process was restarted.

        A symbol already holding bars is kept even when its top-up fails. Its
        bars go stale -- which the relative-strength quality flags already
        report -- and stale is strictly better than dropping it, because a
        quietly shrinking panel corrupts every cross-sectional rank drawn from
        it.
        """
        interval = self.scanner_config.interval_seconds
        admitted: list[str] = []
        excluded: dict[str, str] = {}
        for symbol in candidates:
            failure: Optional[str] = None
            try:
                for cache, label, step, depth in (
                    (self.hourly, "1h", interval, self.live_config.hourly_window),
                    (self.daily, "1d", DAY_SECONDS, self.live_config.daily_window),
                ):
                    last = cache.last_timestamp(symbol)
                    if last is None:
                        start = as_of - timedelta(seconds=step * depth)
                    else:
                        # Re-request the stored tail so a bar captured mid-formation
                        # is replaced by its settled version.
                        start = last - timedelta(seconds=step * 2)
                    cache.merge(symbol, self.fetcher.fetch(symbol, label, start, as_of))
            except Exception as exc:  # noqa: BLE001 - one symbol must not cost the panel
                failure = f"{type(exc).__name__}: {exc}"
            if len(completed_bars(self.daily.get(symbol), DAY_SECONDS, as_of)) >= MIN_DAILY_BARS:
                admitted.append(symbol)
            else:
                excluded[symbol] = failure or (
                    f"fewer than {MIN_DAILY_BARS} completed daily bars cached"
                )
        if self.scanner_config.benchmark not in admitted:
            raise UniverseError(
                f"benchmark {self.scanner_config.benchmark} has no usable candles "
                f"({excluded.get(self.scanner_config.benchmark, 'absent from the universe')}); "
                "a panel without it has no comparable cross-section"
            )
        with self._lock:
            self._excluded = excluded
        return admitted

    # -- the tick ---------------------------------------------------------

    def tick(self, as_of: datetime, refresh_candles: bool) -> None:
        """Run one scan. A failure is recorded, never written as a partial panel."""
        try:
            if refresh_candles or not self.symbols:
                # Only the admitted subset is ever published as scannable.
                self.symbols = self._refresh_candles(self._refresh_universe(), as_of)
            mids = {
                symbol: float(price)
                for symbol, price in self.source.fetch_mids().items()
                if symbol in set(self.symbols)
            }
            panel = build_panel(
                symbols=self.symbols, hourly=self.hourly,
                daily={symbol: self.daily.get(symbol) for symbol in self.symbols},
                mids=mids, prior_prices=self.prior_prices, as_of=as_of,
                interval_seconds=self.scanner_config.interval_seconds,
                benchmark=self.scanner_config.benchmark,
            )
            rows = scan_assets(panel, as_of, self.scanner_config)
            snapshot = scan_snapshot(as_of, rows, self.scanner_config)
            self._publish(snapshot)
            self.prior_prices = {symbol: asset.price for symbol, asset in panel.items()}
        except Exception as exc:  # noqa: BLE001 - a loop must survive any feed fault
            with self._lock:
                self._failures += 1
                self._error = f"{type(exc).__name__}: {exc}"
            return
        with self._lock:
            self._failures = 0
            self._error = None
            self._last_tick = as_of
            if refresh_candles:
                self._last_candle_refresh = as_of

    def _publish(self, snapshot: Mapping[str, Any]) -> None:
        snapshot_path = self.output_dir / "scanner_latest.json"
        history_path = self.output_dir / "signal_history.jsonl"
        try:
            previous = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else None
        except (OSError, json.JSONDecodeError):
            previous = None
        events = signal_transitions(previous, snapshot)
        write_json_atomic(snapshot_path, snapshot)
        append_history(history_path, events)

    def status(self) -> LiveStatus:
        with self._lock:
            return LiveStatus(
                running=self._thread is not None and self._thread.is_alive(),
                universe_size=len(self.symbols),
                last_tick=self._last_tick.isoformat() if self._last_tick else None,
                last_candle_refresh=(
                    self._last_candle_refresh.isoformat() if self._last_candle_refresh else None
                ),
                next_candle_refresh=(
                    self._next_candle_refresh.isoformat() if self._next_candle_refresh else None
                ),
                consecutive_failures=self._failures,
                last_error=self._error,
                excluded_symbols=dict(self._excluded),
            )

    # -- threading --------------------------------------------------------

    def _next_boundary(self, now: datetime) -> datetime:
        """Just after the next bar close, when new candles actually exist."""
        step = self.scanner_config.interval_seconds
        epoch_seconds = now.timestamp()
        next_close = (int(epoch_seconds // step) + 1) * step
        return datetime.fromtimestamp(next_close, tz=timezone.utc) + timedelta(
            seconds=self.live_config.settle_seconds
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            now = datetime.now(timezone.utc)
            due = self._next_candle_refresh is None or now >= self._next_candle_refresh
            self.tick(now, refresh_candles=due)
            if due:
                with self._lock:
                    self._next_candle_refresh = self._next_boundary(now)
            self._stop.wait(self.live_config.fast_interval_seconds)

    def start(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="terra-live", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
