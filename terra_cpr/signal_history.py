"""Persistent, non-spamming scanner signal transition history."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import closing
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Sequence


ALERT_LABELS = frozenset({"LONG_CANDIDATE", "SHORT_CANDIDATE"})


def _candidate_state(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    setup = row.get("setup", {})
    # Mid-price candidates are deliberately visible in the cockpit but never
    # enter the durable research event stream. Only a completed-bar state can
    # activate, change, or clear a confirmed signal.
    if (
        setup.get("label") not in ALERT_LABELS
        or setup.get("confirmation") != "CONFIRMED"
    ):
        return None
    rs = row.get("rs", {})
    market = row.get("market", {})
    return {
        "label": setup.get("label"), "direction": setup.get("direction"),
        "confirmation": setup.get("confirmation"),
        "strength": setup.get("strength"), "score": rs.get("score"),
        "reasons": setup.get("reasons", []),
        "cpr_position": market.get("price_cpr_position"),
        "pivot_position": market.get("pivot_position"),
    }


def signal_transitions(previous: Mapping[str, Any] | None, current: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Generate candidate state transitions without treating score refreshes as alerts."""
    prior_rows = {row["symbol"]: row for row in (previous or {}).get("rows", [])}
    current_rows = {row["symbol"]: row for row in current.get("rows", [])}
    events: list[dict[str, Any]] = []
    for symbol in sorted(set(prior_rows).union(current_rows)):
        before, after = _candidate_state(prior_rows.get(symbol)), _candidate_state(current_rows.get(symbol))
        if before is None and after is not None:
            event = "activated"
        elif before is not None and after is None:
            event = "cleared"
        elif before is not None and after is not None and (before["label"], before["direction"]) != (after["label"], after["direction"]):
            event = "changed"
        else:
            continue
        events.append({
            "timestamp": current["as_of"], "symbol": symbol, "event": event,
            "current": after, "previous": before,
        })
    return events


def append_history(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        handle.flush()


def read_history(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    """Return the newest valid events first; a torn trailing line is ignored."""
    database = path.with_suffix(".sqlite3")
    if database.exists() and limit > 0:
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            return [json.loads(row[0]) for row in connection.execute(
                "SELECT payload FROM signal_events ORDER BY id DESC LIMIT ?", (limit,)
            )]
    if limit <= 0 or not path.exists():
        return []
    events = deque(maxlen=limit)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
    return list(reversed(events))


class SignalStore:
    """Confirmed state and its transitions share one durable commit.

    The JSONL is a compatibility export, never the transition checkpoint.
    Missing/unavailable rows and fast ticks cannot clear a confirmed state.
    """

    def __init__(self, output: Path):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.path = self.output / "signal_history.sqlite3"
        fresh = not self.path.exists()
        self.dirty_export = True
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS signal_states (
                    symbol TEXT PRIMARY KEY, cutoff TEXT NOT NULL, row_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS signal_events (
                    id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            """)
            if fresh:
                legacy = self.output / "signal_history.jsonl"
                if legacy.exists():
                    with legacy.open() as handle:
                        for line in handle:
                            try:
                                event = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(event, dict):
                                db.execute("INSERT INTO signal_events(payload) VALUES (?)", (json.dumps(event),))
                snapshot = self.output / "scanner_latest.json"
                if snapshot.exists():
                    try:
                        old = json.loads(snapshot.read_text())
                        for row in old.get("rows", []):
                            db.execute("INSERT OR IGNORE INTO signal_states VALUES (?, ?, ?)",
                                       (row["symbol"], row.get("input_cutoff") or old["as_of"], json.dumps(row)))
                    except (ValueError, KeyError):
                        pass

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def record(self, snapshot, completed_refresh=True):
        if not completed_refresh:
            return []
        from .report import _json_default
        snapshot = json.loads(json.dumps(snapshot, default=_json_default))
        events = []
        with closing(self._connect()) as db, db:
            for row in snapshot["rows"]:
                if not row.get("data_available", True):
                    continue
                cutoff = row.get("input_cutoff") or snapshot["as_of"]
                prior = db.execute("SELECT cutoff, row_json FROM signal_states WHERE symbol=?", (row["symbol"],)).fetchone()
                if prior and cutoff <= prior[0]:
                    continue
                previous = {"rows": [json.loads(prior[1])]} if prior else None
                new_events = signal_transitions(previous, {"as_of": cutoff, "rows": [row]})
                for event in new_events:
                    event["observed_at"] = snapshot.get("observed_at", snapshot["as_of"])
                    db.execute("INSERT INTO signal_events(payload) VALUES (?)", (json.dumps(event),))
                events.extend(new_events)
                db.execute("INSERT OR REPLACE INTO signal_states VALUES (?, ?, ?)", (row["symbol"], cutoff, json.dumps(row)))
        self.dirty_export = self.dirty_export or bool(events)
        return events

    def export(self):
        if not self.dirty_export:
            return
        fd, name = tempfile.mkstemp(dir=self.output, prefix=".signals-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle, closing(self._connect()) as db:
                for row in db.execute("SELECT id, payload FROM signal_events ORDER BY id"):
                    payload = json.loads(row[1])
                    payload["event_id"] = row[0]
                    handle.write(json.dumps(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.output / "signal_history.jsonl")
            self.dirty_export = False
        finally:
            if os.path.exists(name):
                os.unlink(name)
