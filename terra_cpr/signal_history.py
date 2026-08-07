"""Persistent, non-spamming scanner signal transition history."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


ALERT_LABELS = frozenset({"LONG_CANDIDATE", "SHORT_CANDIDATE"})


def _candidate_state(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    setup = row.get("setup", {})
    if setup.get("label") not in ALERT_LABELS:
        return None
    rs = row.get("rs", {})
    market = row.get("market", {})
    return {
        "label": setup.get("label"), "direction": setup.get("direction"),
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
    if limit <= 0 or not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return list(reversed(events[-limit:]))
