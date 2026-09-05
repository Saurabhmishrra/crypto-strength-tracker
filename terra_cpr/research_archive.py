"""Durable point-in-time archive for completed panels and frozen research rules."""
from __future__ import annotations

import json
import hashlib
from contextlib import closing
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import ScanRow
from .research import (
    BROAD_ALT_FACTOR_MODEL,
    CURRENT_FACTOR_MODEL,
    FIVE_MODEL_COMPARISON,
    ResearchSpecification,
    ResearchTrigger,
    active_research_triggers,
    research_row_eligible,
)
from .scanner import AssetInput, ScannerConfig
from .report import _json_default
from .provenance import source_identity


ARCHIVE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ResearchArchiveStatus:
    path: str
    schema_version: int
    last_completed_bar: str | None
    scans: int
    candles: int
    panel_rows: int
    evaluations: int
    observations: int
    events: int
    active_states: int
    models: Mapping[str, Mapping[str, Any]]
    unavailable_states: int = 0


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default, allow_nan=False)


def _trigger_payload(trigger: ResearchTrigger) -> dict:
    return {
        "event_rule": trigger.event_rule,
        "symbol": trigger.symbol,
        "direction": trigger.direction,
        "state_label": trigger.state_label,
        "score": trigger.score,
        "entry_price": trigger.entry_price,
        "model_version": trigger.model_version,
        "features": dict(trigger.features),
    }


class ResearchArchive:
    """SQLite event store written only after a completed candle refresh.

    Completed candles are deduplicated by symbol, interval, and open timestamp.
    Every scan stores its actual panel membership and point-in-time context. Rule
    observations and transitions use the same pure evaluator as historical replay.
    A fresh connection per write keeps the object safe when the live loop and HTTP
    status endpoint run on different threads.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialise()
        self._status = self._read_status()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialise(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS archive_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL UNIQUE,
                    interval_seconds INTEGER NOT NULL,
                    universe_json TEXT NOT NULL,
                    gates_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (symbol, interval_seconds, timestamp)
                );

                CREATE TABLE IF NOT EXISTS panel_rows (
                    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    symbol TEXT NOT NULL,
                    price REAL NOT NULL,
                    prior_price REAL,
                    session_open REAL,
                    context_json TEXT,
                    PRIMARY KEY (scan_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS research_observations (
                    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    spec_id TEXT NOT NULL,
                    spec_label TEXT NOT NULL,
                    event_rule TEXT NOT NULL,
                    factor_model TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    state_label TEXT NOT NULL,
                    score REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    PRIMARY KEY (scan_id, spec_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS research_evaluations (
                    scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                    spec_id TEXT NOT NULL,
                    spec_label TEXT NOT NULL,
                    event_rule TEXT NOT NULL,
                    factor_model TEXT NOT NULL,
                    evaluated_rows INTEGER NOT NULL,
                    usable_rows INTEGER NOT NULL,
                    factor_available_rows INTEGER NOT NULL,
                    active_states INTEGER NOT NULL,
                    model_versions_json TEXT NOT NULL,
                    PRIMARY KEY (scan_id, spec_id)
                );

                CREATE TABLE IF NOT EXISTS research_states (
                    spec_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_as_of TEXT NOT NULL,
                    PRIMARY KEY (spec_id, symbol)
                );

                CREATE TABLE IF NOT EXISTS research_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    spec_id TEXT NOT NULL,
                    spec_label TEXT NOT NULL,
                    event_rule TEXT NOT NULL,
                    factor_model TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    current_json TEXT,
                    previous_json TEXT,
                    UNIQUE (as_of, spec_id, symbol, event_type)
                );

                CREATE INDEX IF NOT EXISTS idx_events_spec_time
                    ON research_events(spec_id, as_of);
                CREATE INDEX IF NOT EXISTS idx_observations_spec_time
                    ON research_observations(spec_id, scan_id);
            """)
            stored_version = connection.execute(
                "SELECT value FROM archive_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if (
                stored_version is not None
                and int(stored_version[0]) not in (1, ARCHIVE_SCHEMA_VERSION)
            ):
                raise RuntimeError(
                    "unsupported research archive schema version "
                    f"{stored_version[0]}; expected {ARCHIVE_SCHEMA_VERSION}"
                )
            # Additive migration preserves legacy history; unknown historical
            # availability times/configs remain NULL and cannot be replayed.
            additions = {
                "scans": {"observed_at": "TEXT", "evaluated_at": "TEXT", "config_json": "TEXT", "config_hash": "TEXT", "source_hash": "TEXT", "code_revision": "TEXT", "selection_json": "TEXT"},
                "panel_rows": {"observed_at": "TEXT", "input_json": "TEXT", "factor_diagnostics_json": "TEXT"},
                "research_evaluations": {"unavailable_rows": "INTEGER NOT NULL DEFAULT 0"},
            }
            for table, columns in additions.items():
                existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                for name, definition in columns.items():
                    if name not in existing:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            connection.execute("""CREATE TABLE IF NOT EXISTS candle_versions (
                symbol TEXT NOT NULL, interval_seconds INTEGER NOT NULL, timestamp TEXT NOT NULL,
                first_scan_id INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL,
                PRIMARY KEY(symbol, interval_seconds, timestamp, first_scan_id))""")
            if stored_version is not None and int(stored_version[0]) == 1:
                connection.execute("""INSERT OR IGNORE INTO candle_versions
                    SELECT symbol, interval_seconds, timestamp, 0, open, high, low, close, volume FROM candles""")
            connection.execute(
                "INSERT OR REPLACE INTO archive_metadata(key, value) VALUES (?, ?)",
                ("schema_version", str(ARCHIVE_SCHEMA_VERSION)),
            )
            connection.commit()
            connection.execute(
                "INSERT OR IGNORE INTO archive_metadata(key, value) VALUES (?, ?)",
                (
                    "frozen_specifications",
                    _json([asdict(spec) for spec in FIVE_MODEL_COMPARISON]),
                ),
            )
            connection.commit()
            check = connection.execute("PRAGMA quick_check").fetchone()
            if check is None or check[0] != "ok":
                raise RuntimeError(f"research archive integrity check failed: {check}")

    def _read_status(self) -> ResearchArchiveStatus:
        with closing(self._connect()) as connection:
            last = connection.execute("SELECT MAX(as_of) FROM scans").fetchone()[0]

            def count(table: str) -> int:
                return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

            models: dict[str, dict[str, Any]] = {}
            for specification in FIVE_MODEL_COMPARISON:
                observations = int(connection.execute(
                    "SELECT COUNT(*) FROM research_observations WHERE spec_id = ?",
                    (specification.spec_id,),
                ).fetchone()[0])
                events = int(connection.execute(
                    "SELECT COUNT(*) FROM research_events WHERE spec_id = ?",
                    (specification.spec_id,),
                ).fetchone()[0])
                event_types = {
                    event_type: int(total)
                    for event_type, total in connection.execute(
                        """SELECT event_type, COUNT(*) FROM research_events
                           WHERE spec_id = ? GROUP BY event_type""",
                        (specification.spec_id,),
                    )
                }
                active = int(connection.execute(
                    "SELECT COUNT(*) FROM research_states WHERE spec_id = ?",
                    (specification.spec_id,),
                ).fetchone()[0])
                evaluations = int(connection.execute(
                    "SELECT COUNT(*) FROM research_evaluations WHERE spec_id = ?",
                    (specification.spec_id,),
                ).fetchone()[0])
                models[specification.spec_id] = {
                    "label": specification.label,
                    "evaluations": evaluations,
                    "observations": observations,
                    "events": events,
                    "activations": event_types.get("activated", 0),
                    "changes": event_types.get("changed", 0),
                    "clearings": event_types.get("cleared", 0),
                    "seeded": event_types.get("seeded", 0),
                    "active_states": active,
                    "unavailable_states": int(connection.execute(
                        "SELECT COUNT(*) FROM research_states WHERE spec_id=? AND updated_as_of<?",
                        (specification.spec_id, last or ""),
                    ).fetchone()[0]),
                }

            return ResearchArchiveStatus(
                path=str(self.path),
                schema_version=ARCHIVE_SCHEMA_VERSION,
                last_completed_bar=last,
                scans=count("scans"),
                candles=count("candles"),
                panel_rows=count("panel_rows"),
                evaluations=count("research_evaluations"),
                observations=count("research_observations"),
                events=count("research_events"),
                active_states=count("research_states"),
                models=models,
                unavailable_states=int(connection.execute(
                    "SELECT COUNT(*) FROM research_states WHERE updated_as_of<?", (last or "",)
                ).fetchone()[0]),
            )

    def status(self) -> ResearchArchiveStatus:
        with self._lock:
            return self._status

    @staticmethod
    def completed_bar_time(
        panel: Mapping[str, AssetInput], scanner_config: ScannerConfig
    ) -> datetime:
        benchmark = panel.get(scanner_config.benchmark)
        if benchmark is None or not benchmark.intraday:
            raise ValueError("research archive requires a completed benchmark candle")
        return benchmark.intraday[-1].timestamp + timedelta(
            seconds=scanner_config.interval_seconds
        )

    @staticmethod
    def _candles(panel: Mapping[str, AssetInput], interval_seconds: int):
        for symbol, asset in panel.items():
            for candle in asset.intraday:
                yield symbol, interval_seconds, candle
            for candle in asset.daily:
                yield symbol, 86_400, candle

    @staticmethod
    def _spec_triggers(
        rows_by_factor: Mapping[str, Sequence[ScanRow]],
        scanner_config: ScannerConfig,
    ) -> dict[ResearchSpecification, dict[str, ResearchTrigger]]:
        output = {}
        for factor_model in (CURRENT_FACTOR_MODEL, BROAD_ALT_FACTOR_MODEL):
            specifications = tuple(
                spec for spec in FIVE_MODEL_COMPARISON
                if spec.factor_model == factor_model
            )
            rules = tuple(dict.fromkeys(spec.event_rule for spec in specifications))
            active = active_research_triggers(
                rows_by_factor[factor_model], scanner_config, rules, factor_model
            )
            for specification in specifications:
                output[specification] = active[specification.event_rule]
        return output

    def record(
        self,
        panel: Mapping[str, AssetInput],
        rows_by_factor: Mapping[str, Sequence[ScanRow]],
        scanner_config: ScannerConfig,
        observed_at: datetime | None = None,
        selection: Mapping[str, Any] | None = None,
    ) -> ResearchArchiveStatus:
        """Atomically archive one completed-bar panel and all five model states."""
        as_of = self.completed_bar_time(panel, scanner_config)
        as_of_text = as_of.isoformat()
        availability = [as_of, *[row.rs.as_of for rows in rows_by_factor.values() for row in rows]]
        for asset in panel.values():
            availability.extend(stamp for stamp in (
                asset.price_observed_at, asset.candles_observed_at,
                asset.context.observed_at if asset.context else None,
            ) if stamp is not None)
        observed_at = max([*availability, *([observed_at] if observed_at else [])])
        config_json = _json(asdict(scanner_config))
        config_hash = hashlib.sha256(config_json.encode()).hexdigest()
        identity = source_identity()
        evaluated_at = max((row.rs.as_of for rows in rows_by_factor.values() for row in rows), default=as_of)
        triggers_by_spec = self._spec_triggers(rows_by_factor, scanner_config)
        gates = {
            "candidate_rs_score": scanner_config.candidate_rs_score,
            "candidate_persistence": scanner_config.candidate_persistence,
            "early_discovery_score": scanner_config.early_discovery_score,
            "strong_discovery_score": scanner_config.strong_discovery_score,
        }

        with closing(self._connect()) as connection:
            with connection:
                newest = connection.execute("SELECT MAX(as_of) FROM scans").fetchone()[0]
                if newest is not None and as_of_text < newest:
                    raise ValueError(
                        f"research archive refuses out-of-order scan {as_of_text}; "
                        f"newest stored scan is {newest}"
                    )
                if newest is not None:
                    last_hash = connection.execute("SELECT config_hash FROM scans WHERE as_of=?", (newest,)).fetchone()[0]
                    if last_hash and last_hash != config_hash:
                        raise ValueError("archive configuration changed; use a separate output directory for a new experiment")
                if newest == as_of_text:
                    stored_hash = connection.execute("SELECT config_hash FROM scans WHERE as_of=?", (as_of_text,)).fetchone()[0]
                    if stored_hash and stored_hash != config_hash:
                        raise ValueError("same bar already archived with a different configuration")
                    return self.status()
                connection.execute(
                    """INSERT INTO scans(
                           as_of, interval_seconds, universe_json, gates_json,
                           observed_at, config_json, config_hash, source_hash, code_revision, selection_json, evaluated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        as_of_text,
                        scanner_config.interval_seconds,
                        _json(sorted(panel)),
                        _json(gates), observed_at.isoformat(), config_json, config_hash,
                        identity["source_hash"], identity["code_revision"], _json(selection or {"admitted": sorted(panel)}), evaluated_at.isoformat(),
                    ),
                )
                scan_id = int(connection.execute(
                    "SELECT id FROM scans WHERE as_of = ?", (as_of_text,)
                ).fetchone()[0])
                candle_rows = [
                    (
                        symbol, interval, candle.timestamp.isoformat(),
                        candle.open, candle.high, candle.low, candle.close,
                        candle.volume,
                    )
                    for symbol, interval, candle in self._candles(
                        panel, scanner_config.interval_seconds
                    )
                ]
                # Preserve each observed revision so later exchange corrections
                # cannot rewrite the inputs used by an earlier scan.
                for values in candle_rows:
                    symbol, interval, timestamp, *ohlcv = values
                    old = connection.execute("SELECT open, high, low, close, volume FROM candles WHERE symbol=? AND interval_seconds=? AND timestamp=?", values[:3]).fetchone()
                    if old is None or tuple(ohlcv) != old:
                        connection.execute("INSERT INTO candle_versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                           (symbol, interval, timestamp, scan_id, *ohlcv))
                        connection.execute("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)", values)

                connection.execute("DELETE FROM panel_rows WHERE scan_id = ?", (scan_id,))
                connection.executemany(
                    """INSERT INTO panel_rows(
                           scan_id, symbol, price, prior_price, session_open,
                           context_json, observed_at, input_json, factor_diagnostics_json
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            scan_id, symbol, asset.price, asset.prior_price,
                            asset.session_open,
                            _json(asdict(asset.context)) if asset.context else None,
                            (asset.price_observed_at or observed_at).isoformat(),
                            _json({
                                "intraday_timestamps": [c.timestamp for c in asset.intraday],
                                "daily_timestamps": [c.timestamp for c in asset.daily],
                                "candles_observed_at": asset.candles_observed_at,
                                "price_observed_at": asset.price_observed_at,
                            }),
                            _json({model: {"quality_flags": row.rs.quality_flags,
                                          "daily_quality_flags": row.market.quality_flags,
                                          "input_cutoff": row.input_cutoff,
                                          "beta": asdict(row.rs.beta) if row.rs.beta else None,
                                          "model_version": row.rs.model_version}
                                   for model, model_rows in rows_by_factor.items()
                                   for row in model_rows if row.symbol == symbol}),
                        )
                        for symbol, asset in sorted(panel.items())
                    ],
                )
                connection.execute(
                    "DELETE FROM research_observations WHERE scan_id = ?", (scan_id,)
                )
                connection.execute(
                    "DELETE FROM research_evaluations WHERE scan_id = ?", (scan_id,)
                )

                for specification, current in triggers_by_spec.items():
                    factor_rows = rows_by_factor[specification.factor_model]
                    eligible_symbols = {row.symbol for row in factor_rows if research_row_eligible(row, specification.event_rule, specification.factor_model)}
                    usable_rows = len(eligible_symbols)
                    factor_available_rows = sum(
                        row.symbol in eligible_symbols and row.rs.beta is not None and row.rs.beta.secondary_beta is not None
                        for row in factor_rows
                    )
                    connection.execute(
                        """INSERT INTO research_evaluations(
                               scan_id, spec_id, spec_label, event_rule,
                               factor_model, evaluated_rows, usable_rows,
                               factor_available_rows, active_states,
                               model_versions_json, unavailable_rows
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            scan_id, specification.spec_id, specification.label,
                            specification.event_rule, specification.factor_model,
                            len(factor_rows), usable_rows, factor_available_rows,
                            len(current),
                            _json(sorted({row.rs.model_version for row in factor_rows})),
                            max(len(panel) - 1, len(factor_rows)) - usable_rows,
                        ),
                    )
                    had_spec_history = connection.execute(
                        "SELECT 1 FROM research_observations WHERE spec_id = ? LIMIT 1",
                        (specification.spec_id,),
                    ).fetchone() is not None
                    previous = {
                        symbol: json.loads(state_json)
                        for symbol, state_json in connection.execute(
                            "SELECT symbol, state_json FROM research_states "
                            "WHERE spec_id = ?",
                            (specification.spec_id,),
                        )
                    }
                    current_payloads = {
                        symbol: _trigger_payload(trigger)
                        for symbol, trigger in current.items()
                    }
                    previous_times = dict(connection.execute(
                        "SELECT symbol, updated_as_of FROM research_states WHERE spec_id=?", (specification.spec_id,)
                    ))
                    connection.executemany(
                        """INSERT INTO research_observations(
                               scan_id, spec_id, spec_label, event_rule,
                               factor_model, model_version, symbol, direction, state_label,
                               score, entry_price, features_json
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            (
                                scan_id, specification.spec_id,
                                specification.label, specification.event_rule,
                                specification.factor_model,
                                payload["model_version"], symbol,
                                payload["direction"], payload["state_label"],
                                payload["score"], payload["entry_price"],
                                _json(payload["features"]),
                            )
                            for symbol, payload in sorted(current_payloads.items())
                        ],
                    )

                    # An unavailable or temporarily absent observation is not a
                    # valid clearing. Keep its durable state until evaluated.
                    retained = {symbol: payload for symbol, payload in previous.items() if symbol not in eligible_symbols}
                    for symbol in sorted(set(previous).union(current_payloads)):
                        if symbol not in eligible_symbols:
                            continue
                        before = previous.get(symbol)
                        after = current_payloads.get(symbol)
                        if before is None and after is not None:
                            event_type = "activated" if had_spec_history else "seeded"
                        elif before is not None and after is None:
                            event_type = "cleared"
                        elif (
                            before is not None and after is not None
                            and (before["state_label"], before["direction"])
                            != (after["state_label"], after["direction"])
                        ):
                            event_type = "changed"
                        else:
                            continue
                        connection.execute(
                            """INSERT OR IGNORE INTO research_events(
                                   as_of, spec_id, spec_label, event_rule,
                                   factor_model, symbol, event_type,
                                   current_json, previous_json
                               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                as_of_text, specification.spec_id,
                                specification.label, specification.event_rule,
                                specification.factor_model, symbol, event_type,
                                _json(after) if after is not None else None,
                                _json(before) if before is not None else None,
                            ),
                        )

                    current_payloads = {**retained, **current_payloads}
                    connection.execute(
                        "DELETE FROM research_states WHERE spec_id = ?",
                        (specification.spec_id,),
                    )
                    connection.executemany(
                        """INSERT INTO research_states(
                               spec_id, symbol, state_json, updated_as_of
                           ) VALUES (?, ?, ?, ?)""",
                        [
                            (
                                specification.spec_id, symbol, _json(payload),
                                previous_times[symbol] if symbol in retained else as_of_text,
                            )
                            for symbol, payload in sorted(current_payloads.items())
                        ],
                    )

        status = self._read_status()
        with self._lock:
            self._status = status
        return status
