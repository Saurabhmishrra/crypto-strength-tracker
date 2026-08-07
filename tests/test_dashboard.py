"""The dashboard's HTTP surface must stay read-only, loopback-only, and offline."""
from __future__ import annotations

import inspect
import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from terra_cpr.dashboard import _Handler, dashboard_html, serve
from terra_cpr.live import LiveStatus

SNAPSHOT = {
    "schema_version": 2,
    "as_of": "2026-08-07T05:00:00+00:00",
    "gates": {"candidate_rs_score": 3.5, "candidate_persistence": 0.40},
    "rows": [{"symbol": "SOL", "setup": {"label": "WATCH"}}],
}


class _FakeScanner:
    def __init__(self, failures=0, error=None):
        self._status = LiveStatus(
            running=True, universe_size=42, last_tick="2026-08-07T05:00:00+00:00",
            last_candle_refresh=None, next_candle_refresh=None,
            consecutive_failures=failures, last_error=error,
        )

    def status(self):
        return self._status


class _ServerCase(unittest.TestCase):
    live_scanner = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        handler = type(
            "TestHandler", (_Handler,),
            {"output_dir": self.output, "live_scanner": self.live_scanner},
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), thread.join(timeout=5)))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _write_snapshot(self):
        (self.output / "scanner_latest.json").write_text(json.dumps(SNAPSHOT))

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read().decode()

    def status_of(self, path, method="GET", body=None):
        request = urllib.request.Request(self.base + path, method=method, data=body)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code


class ReadRoutes(_ServerCase):
    def test_root_serves_the_cockpit(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("<!doctype html>", body)

    def test_snapshot_is_returned_verbatim(self):
        self._write_snapshot()
        status, body = self.get("/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), SNAPSHOT)

    def test_snapshot_is_unavailable_before_the_first_scan(self):
        self.assertEqual(self.status_of("/api/snapshot"), 503)

    def test_health_reports_whether_a_snapshot_exists(self):
        self.assertFalse(json.loads(self.get("/health")[1])["ok"])
        self._write_snapshot()
        self.assertTrue(json.loads(self.get("/health")[1])["ok"])

    def test_signals_returns_newest_first_within_the_limit(self):
        path = self.output / "signal_history.jsonl"
        path.write_text("".join(
            json.dumps({"symbol": f"A{i}", "event": "activated"}) + "\n" for i in range(5)
        ))
        events = json.loads(self.get("/api/signals?limit=2")[1])
        self.assertEqual([e["symbol"] for e in events], ["A4", "A3"])

    def test_a_nonsense_limit_does_not_break_the_feed(self):
        self.assertEqual(self.status_of("/api/signals?limit=banana"), 200)

    def test_unknown_paths_are_not_found(self):
        self.assertEqual(self.status_of("/etc/passwd"), 404)


class WriteMethodsRejected(_ServerCase):
    def test_every_mutating_method_is_refused(self):
        for method in ("POST", "PUT", "DELETE"):
            for path in ("/", "/api/snapshot", "/api/status"):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.status_of(path, method, b""), 405)


class StatusWithoutLoop(_ServerCase):
    def test_loop_off_is_distinguishable_from_a_failure(self):
        payload = json.loads(self.get("/api/status")[1])
        self.assertEqual(payload["loop"], "off")
        self.assertNotIn("consecutive_failures", payload)


class StatusWithHealthyLoop(_ServerCase):
    live_scanner = _FakeScanner()

    def test_a_healthy_loop_reports_live(self):
        payload = json.loads(self.get("/api/status")[1])
        self.assertEqual(payload["loop"], "live")
        self.assertEqual(payload["universe_size"], 42)


class StatusWithFailingLoop(_ServerCase):
    live_scanner = _FakeScanner(failures=3, error="RuntimeError: endpoint down")

    def test_a_failing_loop_is_reported_as_failing_with_its_error(self):
        payload = json.loads(self.get("/api/status")[1])
        self.assertEqual(payload["loop"], "failing")
        self.assertEqual(payload["consecutive_failures"], 3)
        self.assertIn("endpoint down", payload["last_error"])


class SafetyBoundary(unittest.TestCase):
    def test_serve_exposes_no_bind_address_parameter(self):
        """Binding beyond loopback must require editing the source, not a flag."""
        parameters = set(inspect.signature(serve).parameters)
        self.assertEqual(parameters, {"output_dir", "port", "live_scanner"})

    def test_serve_binds_loopback_in_its_source(self):
        self.assertIn('"127.0.0.1"', inspect.getsource(serve))

    def test_the_page_loads_nothing_from_the_network(self):
        """Zero-dependency means zero: no CDN, font, image, or script host."""
        html = dashboard_html()
        self.assertEqual(re.findall(r'(?:src|href)\s*=\s*"(?!#)[^"]*"', html), [])
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_the_page_offers_no_trading_controls(self):
        """Research has not earned an order ticket. Word boundaries keep this
        honest: a bare 'order' substring would match 'border' in the stylesheet."""
        html = dashboard_html().lower()
        self.assertNotIn("<form", html)
        for word in ("order", "buy", "sell", "wallet", "leverage", "withdraw"):
            with self.subTest(word=word):
                self.assertIsNone(re.search(rf"\b{word}\b", html))

    def test_the_page_only_ever_issues_get_requests(self):
        html = dashboard_html()
        self.assertNotIn("XMLHttpRequest", html)
        for verb in ('"POST"', "'POST'", '"PUT"', '"DELETE"', "method:"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, html)


if __name__ == "__main__":
    unittest.main()
