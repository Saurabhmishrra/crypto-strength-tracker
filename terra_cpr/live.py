"""Live public-market panel construction for Strength Tracker.

This module reads public Hyperliquid market data and assembles the same
``AssetInput`` panel the fixture adapter produces. It holds no credentials, no
signing code, and no order path, and it never mutates a scanner engine.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .data import completed_bars
from .models import Candle, MarketContext
from .report import scan_snapshot, write_json_atomic
from .research import (
    BROAD_ALT_FACTOR_MODEL,
    CURRENT_FACTOR_MODEL,
    config_for_factor_model,
)
from .research_archive import ResearchArchive
from .scanner import AssetInput, ScannerConfig, scan_assets
from .signal_history import append_history, signal_transitions

DAY_SECONDS = 86_400
MAX_PUBLIC_CANDLES = 5_000

HYPERLIQUID_INTERVALS = {
    60: "1m",
    180: "3m",
    300: "5m",
    900: "15m",
    1_800: "30m",
    3_600: "1h",
    7_200: "2h",
    14_400: "4h",
    28_800: "8h",
    43_200: "12h",
}

#: Two completed sessions is the hard floor for a CPR: yesterday defines today's
#: levels and the one before it is needed to place them in context. Below this,
#: ``build_market_structure`` raises rather than guessing.
MIN_DAILY_BARS = 2


def hyperliquid_interval(interval_seconds: int) -> str:
    """Map scanner bar duration to an exact Hyperliquid candle interval."""
    try:
        return HYPERLIQUID_INTERVALS[interval_seconds]
    except KeyError:
        supported = ", ".join(str(value) for value in HYPERLIQUID_INTERVALS)
        raise ValueError(
            f"unsupported live interval_seconds={interval_seconds}; supported values: {supported}"
        ) from None


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
    required_symbols: Sequence[str] = (),
) -> list[str]:
    """Rank tradable perps by volume while retaining required factor markets.

    ``meta['universe']`` and ``contexts`` are positionally aligned lists in the
    public ``metaAndAssetCtxs`` response; that index correspondence is the only
    join available, so a length mismatch is unrecoverable rather than partial.
    """
    if size <= 0:
        raise UniverseError("universe size must be positive")
    required = tuple(dict.fromkeys((benchmark, *required_symbols)))
    if size < len(required):
        raise UniverseError(
            f"universe size {size} cannot retain required symbols: {', '.join(required)}"
        )
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
        if not math.isfinite(volume) or volume < 0:
            raise UniverseError(f"invalid 'dayNtlVlm' for {name}: {volume}")
        ranked.append((volume, name))

    ranked_names = {name for _, name in ranked}
    missing = [symbol for symbol in required if symbol not in ranked_names]
    if missing:
        raise UniverseError(
            f"required symbol(s) absent from the tradable universe: {', '.join(missing)}"
        )

    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = ranked[:size]
    selected_names = {name for _, name in selected}
    for required_symbol in required:
        if required_symbol in selected_names:
            continue
        required_entry = next(item for item in ranked if item[1] == required_symbol)
        replace_at = next(
            index for index in range(len(selected) - 1, -1, -1)
            if selected[index][1] not in required
        )
        selected_names.remove(selected[replace_at][1])
        selected[replace_at] = required_entry
        selected_names.add(required_symbol)
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


def _optional_finite(context: Mapping[str, Any], key: str) -> Optional[float]:
    value = context.get(key)
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def build_market_context(
    raw: Mapping[str, Any], completed_daily: Sequence[Candle]
) -> MarketContext:
    """Normalise Hyperliquid context fields without promoting them to score inputs."""
    day_notional = _optional_finite(raw, "dayNtlVlm")
    mid = _optional_finite(raw, "midPx")
    impact_spread_bps = None
    impact = raw.get("impactPxs")
    if isinstance(impact, Sequence) and not isinstance(impact, (str, bytes)) and len(impact) == 2:
        try:
            bid, ask = float(impact[0]), float(impact[1])
        except (TypeError, ValueError):
            bid = ask = float("nan")
        reference = mid if mid and mid > 0 else (bid + ask) / 2.0
        if all(math.isfinite(value) for value in (bid, ask, reference)) and reference > 0 and ask >= bid:
            impact_spread_bps = (ask - bid) / reference * 10_000.0

    historical_notional = [
        candle.volume * ((candle.high + candle.low + candle.close) / 3.0)
        for candle in completed_daily[-20:]
        if candle.volume > 0
    ]
    relative_notional = None
    if day_notional is not None and historical_notional:
        baseline = statistics.median(historical_notional)
        if baseline > 0:
            relative_notional = day_notional / baseline
    return MarketContext(
        source="hyperliquid_metaAndAssetCtxs",
        day_notional_volume=day_notional,
        relative_notional_volume=relative_notional,
        impact_spread_bps=impact_spread_bps,
        funding_rate=_optional_finite(raw, "funding"),
        open_interest=_optional_finite(raw, "openInterest"),
        premium=_optional_finite(raw, "premium"),
        mark_price=_optional_finite(raw, "markPx"),
        context_mid_price=mid,
    )


def build_panel(
    symbols: Sequence[str],
    hourly: CandleCache,
    daily: Mapping[str, Sequence[Candle]],
    mids: Mapping[str, float],
    prior_prices: Mapping[str, float],
    as_of: datetime,
    interval_seconds: int,
    benchmark: str = "BTC",
    contexts: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> dict[str, AssetInput]:
    """Assemble the scanner panel from cached bars and current mid prices.

    Every bar handed to an engine is filtered through ``completed_bars``; the
    only value taken from an unfinished candle is ``session_open``.
    """
    if benchmark not in mids:
        raise UniverseError(f"no mid price for benchmark {benchmark}; cannot scan")
    contexts = contexts or {}
    panel: dict[str, AssetInput] = {}
    for symbol in symbols:
        price = mids.get(symbol)
        if price is None:
            continue
        daily_bars = list(daily.get(symbol, ()))
        closed_daily = completed_bars(daily_bars, DAY_SECONDS, as_of)
        panel[symbol] = AssetInput(
            symbol=symbol,
            price=float(price),
            daily=closed_daily,
            intraday=completed_bars(hourly.get(symbol), interval_seconds, as_of),
            session_open=_session_open(daily_bars, as_of),
            prior_price=prior_prices.get(symbol),
            context=(
                build_market_context(contexts[symbol], closed_daily)
                if symbol in contexts else None
            ),
        )
    return panel


@dataclass(frozen=True)
class LiveConfig:
    """Cadence and depth settings for the live loop."""

    universe_size: int = 60
    fast_interval_seconds: float = 20.0
    settle_seconds: float = 30.0
    hourly_window: int = 722
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
    research_archive: Mapping[str, Any] = field(default_factory=dict)


class LiveScanner:
    """Two-tier public-data loop writing scanner snapshots.

    The fast tier refreshes mid prices only, because the structure leg of the
    decision rule moves continuously while relative strength cannot change until
    a configured intraday bar closes. The slow tier tops candles up just after each close.
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
        research_archive: Optional[ResearchArchive] = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.source = source
        self.scanner_config = scanner_config
        self.live_config = live_config
        self._intraday_interval = hyperliquid_interval(scanner_config.interval_seconds)
        # A beta over N returns needs N+1 completed closes. The endpoint may
        # also return the forming bar, which the cache must retain until the
        # completion gate removes it, so the raw store needs two extra slots.
        self._intraday_window = max(
            live_config.hourly_window,
            scanner_config.rs.beta_window + 2,
            scanner_config.rs.min_beta_points + 2,
            scanner_config.rs.persistence_window + 2,
            scanner_config.rs.long_horizon_bars + 2,
            scanner_config.rs.long_horizon_bars
            + scanner_config.rs.min_empirical_windows + 2,
        )
        if self._intraday_window > MAX_PUBLIC_CANDLES:
            raise ValueError(
                f"the configured live model needs {self._intraday_window} bars, but "
                f"Hyperliquid candleSnapshot exposes only the most recent "
                f"{MAX_PUBLIC_CANDLES}; use 15m or slower, shorten a preregistered "
                "model window, or supply an archive-backed data source"
            )
        # The active daily observation plus its history and a forming day need
        # the same two-slot allowance.
        self._daily_window = max(
            live_config.daily_window,
            scanner_config.market.width_history + 2,
            scanner_config.market.atr_period + 2,
            scanner_config.market.realized_vol_period + 2,
            MIN_DAILY_BARS + 1,
        )
        self.research_archive = research_archive or ResearchArchive(
            self.output_dir / "research_archive.sqlite3"
        )
        self.fetcher = ThrottledCandleFetcher(
            source=source, min_gap_seconds=live_config.min_gap_seconds,
            sleep=sleep, monotonic=monotonic,
        )
        self.hourly = CandleCache(window=self._intraday_window)
        self.daily = CandleCache(window=self._daily_window)
        self.prior_prices: dict[str, float] = {}
        self._market_contexts: dict[str, Mapping[str, Any]] = {}
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
        secondary = self.scanner_config.rs.secondary_benchmark
        entries = meta.get("universe", [])
        secondary_available = any(
            isinstance(entry, Mapping)
            and entry.get("name") == secondary
            and not entry.get("isDelisted")
            for entry in entries
        )
        selected = select_universe(
            meta,
            contexts,
            self.live_config.universe_size,
            self.scanner_config.benchmark,
            (
                (secondary,)
                if secondary and secondary_available else ()
            ),
        )
        entries = meta.get("universe", [])
        self._market_contexts = {
            entry["name"]: context
            for entry, context in zip(entries, contexts)
            if isinstance(entry, Mapping) and entry.get("name") in selected
        }
        return selected

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
                    (self.hourly, self._intraday_interval, interval, self._intraday_window),
                    (self.daily, "1d", DAY_SECONDS, self._daily_window),
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
        did_refresh_candles = refresh_candles or not self.symbols
        try:
            if did_refresh_candles:
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
                contexts=self._market_contexts,
            )
            rows = scan_assets(panel, as_of, self.scanner_config)
            if did_refresh_candles:
                current_factor_config = config_for_factor_model(
                    self.scanner_config, CURRENT_FACTOR_MODEL
                )
                current_factor_rows = (
                    rows
                    if current_factor_config == self.scanner_config
                    else scan_assets(panel, as_of, current_factor_config)
                )
                broad_config = config_for_factor_model(
                    self.scanner_config, BROAD_ALT_FACTOR_MODEL
                )
                broad_rows = scan_assets(panel, as_of, broad_config)
                self.research_archive.record(
                    panel,
                    {
                        CURRENT_FACTOR_MODEL: current_factor_rows,
                        BROAD_ALT_FACTOR_MODEL: broad_rows,
                    },
                    self.scanner_config,
                )
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
            if did_refresh_candles:
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
        archive = asdict(self.research_archive.status())
        archive["path"] = Path(archive["path"]).name
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
                research_archive=archive,
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
