from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from terra_cpr.signal_history import append_history, read_history, signal_transitions


def snapshot(label: str, direction: str = "LONG") -> dict:
    return {
        "as_of": "2026-08-07T05:00:00+00:00",
        "rows": [{
            "symbol": "SOL", "setup": {"label": label, "direction": direction, "strength": 70, "reasons": ["test"]},
            "rs": {"score": 5.0}, "market": {"price_cpr_position": "above_tc", "pivot_position": "above_r3"},
        }],
    }


class SignalHistoryTests(unittest.TestCase):
    def test_only_candidate_state_changes_emit_events(self) -> None:
        inactive = snapshot("WATCH")
        active = snapshot("LONG_CANDIDATE")
        self.assertEqual([event["event"] for event in signal_transitions(inactive, active)], ["activated"])
        self.assertEqual(signal_transitions(active, active), [])
        self.assertEqual([event["event"] for event in signal_transitions(active, inactive)], ["cleared"])

    def test_history_returns_newest_valid_events_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            events = signal_transitions(snapshot("WATCH"), snapshot("LONG_CANDIDATE"))
            append_history(path, events)
            with path.open("a") as handle:
                handle.write("partial-json")
            loaded = read_history(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["event"], "activated")
