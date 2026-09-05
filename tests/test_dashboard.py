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
    "schema_version": 4,
    "as_of": "2026-08-07T05:00:00+00:00",
    "gates": {
        "candidate_rs_score": 3.5, "candidate_persistence": 0.40,
        "early_discovery_score": 2.5, "strong_discovery_score": 3.0,
    },
    "rows": [{"symbol": "SOL", "setup": {"label": "WATCH"}}],
}


class _FakeScanner:
    def __init__(self, failures=0, error=None):
        self._status = LiveStatus(
            running=True, universe_size=42, last_tick="2026-08-07T05:00:00+00:00",
            last_candle_refresh=None, next_candle_refresh=None,
            consecutive_failures=failures, last_error=error,
            ready=not failures,
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

    def test_ready_requires_a_published_snapshot(self):
        self.assertEqual(self.status_of("/ready"), 503)
        self._write_snapshot()
        self.assertEqual(self.status_of("/ready"), 200)


class StatusWithFailingLoop(_ServerCase):
    live_scanner = _FakeScanner(failures=3, error="RuntimeError: endpoint down")

    def test_a_failing_loop_is_reported_as_failing_with_its_error(self):
        payload = json.loads(self.get("/api/status")[1])
        self.assertEqual(payload["loop"], "failing")
        self.assertEqual(payload["consecutive_failures"], 3)
        self.assertIn("endpoint down", payload["last_error"])

    def test_liveness_survives_failure_but_readiness_does_not(self):
        self._write_snapshot()
        self.assertEqual(self.status_of("/livez"), 200)
        self.assertEqual(self.status_of("/ready"), 503)


class PublicExposureHardening(_ServerCase):
    """Once this binds beyond loopback, the response is its own last defence.

    ``http.server`` ships neither TLS nor abuse handling, so a deployment puts
    a proxy in front of it. These headers are what holds regardless of which
    proxy that turns out to be, or whether someone runs it without one.
    """

    def _headers(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return dict(response.headers)

    def test_no_route_may_be_sniffed_or_framed(self):
        self._write_snapshot()
        for path in ("/", "/api/snapshot", "/api/status"):
            with self.subTest(path=path):
                headers = self._headers(path)
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
                self.assertEqual(headers.get("X-Frame-Options"), "DENY")
                self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_the_page_declares_a_policy_that_forbids_remote_loading(self):
        """The zero-dependency promise is enforced in the markup by review and
        at runtime by this header. A public deployment needs both."""
        policy = self._headers("/").get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'self'", policy)
        self.assertIn("form-action 'none'", policy)

    def test_the_server_does_not_advertise_what_it_runs(self):
        """A banner naming Python and its exact patch version hands a scanner
        the applicable CVE list for free."""
        banner = self._headers("/").get("Server", "")
        self.assertNotIn("Python", banner)
        self.assertNotIn("BaseHTTP", banner)


class SafetyBoundary(unittest.TestCase):
    def test_the_product_uses_the_strength_tracker_name(self):
        html = dashboard_html()
        self.assertIn("Strength Tracker", html)
        self.assertNotIn("Terra CPR", html)

    def test_serve_binds_loopback_unless_told_otherwise(self):
        """Public exposure must be a deliberate argument, never the default.

        The dashboard was loopback-only by construction until it was deployed
        as a public site. What actually protected it was never the bind address
        but that no route mutates anything; binding wider is now allowed, and
        forgetting to ask for it still cannot expose a laptop.
        """
        self.assertEqual(inspect.signature(serve).parameters["host"].default, "127.0.0.1")

    def test_serve_refuses_a_bind_address_it_cannot_honour(self):
        for host in ("", "   "):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    serve(Path("."), host=host)

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

    def test_a_failed_fetch_cannot_leave_the_page_looking_healthy(self):
        """A rejected fetch once left the header painting its start-up state:
        a calm grey 'off' while the loop was failing behind it. 'Cannot reach
        the server' and 'no loop configured' must not render the same."""
        html = dashboard_html()
        refresh = html[html.index("async function refresh()"):]
        refresh = refresh[:refresh.index("\n$(")]
        self.assertIn("try {", refresh)
        self.assertIn("catch", refresh)
        self.assertIn("reachable = false", refresh)
        self.assertIn("unreachable", html)

    def test_a_loop_fault_is_stated_in_words_not_only_a_tooltip(self):
        html = dashboard_html()
        self.assertIn('id="fault"', html)
        self.assertIn("consecutive_failures", html)
        self.assertIn("last_error", html)

    def test_a_shrunken_panel_is_named_on_screen(self):
        """Every rank here is cross-sectional. Dropping a symbol quietly changes
        what every surviving rank means, so the held-out set must be visible."""
        html = dashboard_html()
        self.assertIn("excluded_symbols", html)
        self.assertIn("held out of the cross-section", html)

    def test_the_page_only_ever_issues_get_requests(self):
        html = dashboard_html()
        self.assertNotIn("XMLHttpRequest", html)
        for verb in ('"POST"', "'POST'", '"PUT"', '"DELETE"', "method:"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, html)

    def test_candidate_and_watch_filters_are_distinct_and_counted(self):
        html = dashboard_html()
        for value, label in (
            ("LONG_CANDIDATE", "Long candidates"),
            ("SHORT_CANDIDATE", "Short candidates"),
            ("LONG_WATCH", "Long watch"),
            ("SHORT_WATCH", "Short watch"),
            ("EARLY_DISCOVERY", "Early discovery"),
            ("STRONG_DISCOVERY", "Strong discovery"),
            ("CANDIDATE_GRADE", "Candidate grade"),
            ("WATCH", "Watches"),
        ):
            with self.subTest(value=value):
                self.assertIn(
                    f'data-filter="{value}" data-label="{label}"', html
                )
        self.assertIn("function matchesFilter", html)
        self.assertIn("function renderFilterCounts", html)
        self.assertIn("r.setup.label === 'WATCH' && r.setup.direction === 'LONG'", html)
        self.assertIn("r.setup.label === 'WATCH' && r.setup.direction === 'SHORT'", html)
        self.assertIn("r.setup.discovery_tier === value", html)
        self.assertIn("No ${esc(emptyLabel)} are present in the current snapshot.", html)


if __name__ == "__main__":
    unittest.main()


class OutputDirectoryGuard(unittest.TestCase):
    """A denied write must be reported at startup, not 40 seconds into the loop.

    Twice now an unwritable output directory has surfaced as an opaque
    PermissionError from inside the refresh loop, with a served page that
    looked merely stale. Container volumes are mounted root-owned while the
    image runs unprivileged, so this is the expected first failure on a fresh
    deployment -- it should name the directory and stop.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_a_writable_directory_passes_quietly(self):
        from terra_cpr.cli import require_writable

        require_writable(self.directory / "nested")
        self.assertTrue((self.directory / "nested").is_dir())

    def test_an_unwritable_directory_is_named_and_refused(self):
        import os

        from terra_cpr.cli import require_writable

        locked = self.directory / "locked"
        locked.mkdir()
        os.chmod(locked, 0o500)
        self.addCleanup(os.chmod, locked, 0o700)
        if os.access(locked, os.W_OK):
            self.skipTest("running as a user that ignores the mode bits")
        with self.assertRaises(SystemExit) as caught:
            require_writable(locked)
        self.assertIn(str(locked), str(caught.exception))

    def test_the_probe_leaves_nothing_behind(self):
        from terra_cpr.cli import require_writable

        require_writable(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])
