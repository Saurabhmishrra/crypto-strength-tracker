"""Actual Chromium smoke test. Run with the optional test-browser dependency.

    python tests/browser_smoke.py

Set PLAYWRIGHT_CHROMIUM_EXECUTABLE to use an existing isolated browser binary.
"""
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import sync_playwright, expect
from terra_cpr.dashboard import _Handler
from terra_cpr.data import synthetic_demo_assets
from terra_cpr.report import scan_snapshot, write_json_atomic
from terra_cpr.scanner import scan_assets


def main():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        as_of, _, panel = synthetic_demo_assets()
        snapshot = scan_snapshot(as_of, scan_assets(panel, as_of))
        snapshot["as_of"] = datetime.now(timezone.utc).isoformat()
        path = output / "scanner_latest.json"
        write_json_atomic(path, snapshot)
        handler = type("BrowserTestHandler", (_Handler,), {"output_dir": output})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                options = {"headless": True}
                if os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
                    options["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_EXECUTABLE"]
                browser = playwright.chromium.launch(**options)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                expect(page.locator('#rows tr[data-symbol]')).to_have_count(2)
                page.locator('#q').fill('SOL')
                expect(page.locator('#rows tr[data-symbol]')).to_have_count(1)
                page.locator('#rows tr[data-symbol="SOL"]').click()
                expect(page.locator('#drawer')).to_have_attribute('data-open', 'true')
                expect(page.locator('#drawer')).to_contain_text('SOL')
                page.get_by_role('button', name='Close', exact=True).click()
                expect(page.locator('#drawer')).to_have_attribute('data-open', 'false')
                page.locator('#q').fill('')
                page.locator('[data-filter="LONG_CANDIDATE"]').click()
                expect(page.locator('#rows tr[data-symbol]')).to_have_count(1)
                # A successful HTTP response containing stale data must still
                # degrade the page, independently of network availability.
                snapshot["as_of"] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
                write_json_atomic(path, snapshot)
                page.reload()
                expect(page.locator('body')).to_have_class('degraded')
                path.unlink()
                page.reload()
                expect(page.locator('#loopText')).to_have_text('unreachable')
                expect(page.locator('#fault')).to_be_visible()
                if errors:
                    raise AssertionError(errors)
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    print("Browser smoke passed: rows, search, filtering, drawer, stale data, and unavailable snapshot; no JavaScript errors.")


if __name__ == '__main__':
    main()
