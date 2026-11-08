"""Browser regressions against a running dashboard.

Install Playwright and Chromium, start the app, then run:
    python -m unittest discover -s tests -v
Optional: DASHBOARD_URL and BROWSER_EXECUTABLE override the local defaults.
All stats requests are mocked; no solve requests are sent.
"""

import os
import unittest

from playwright.sync_api import expect, sync_playwright


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        options = {"headless": True}
        if os.environ.get("BROWSER_EXECUTABLE"):
            options["executable_path"] = os.environ["BROWSER_EXECUTABLE"]
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.mode = "ok"
        self.page.route("**/solver/stats", self.stats)
        self.base = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:8000/")

    def stats(self, route):
        if self.mode == "offline":
            route.abort()
        elif self.mode == "http-error":
            route.fulfill(status=503, json={"detail": "Unavailable"})
        elif self.mode == "invalid-json":
            route.fulfill(status=200, body="not json")
        else:
            route.fulfill(json={
                "total": 7, "ok": 5, "fail": 2, "success_rate": 71,
                "avg_ms": 100, "max_ms": 200, "history": [], "models": [],
            })

    def assert_page(self, name, title):
        expect(self.page.locator("#ptitle")).to_have_text(title)
        expect(self.page.locator(".page:visible")).to_have_attribute("id", "page-" + name)
        expect(self.page.locator('[aria-current="page"]')).to_have_attribute("href", "#" + name)

    def test_deep_link_reload_and_browser_history(self):
        self.page.goto(self.base + "#history")
        self.assert_page("history", "History")
        self.page.reload()
        self.assert_page("history", "History")
        self.page.locator('[href="#models"]').click()
        self.assert_page("models", "Models")
        self.page.go_back()
        self.assert_page("history", "History")
        self.page.go_forward()
        self.assert_page("models", "Models")
        self.page.goto(self.base + "#unknown")
        self.assert_page("overview", "Overview")

    def test_sidebar_is_keyboard_navigable(self):
        self.page.goto(self.base)
        for name, title in [("overview", "Overview"), ("docs", "Docs"),
                            ("playground", "Playground"), ("history", "History"),
                            ("models", "Models"), ("engines", "Engine")]:
            self.page.keyboard.press("Tab")
            expect(self.page.locator('[href="#' + name + '"]')).to_be_focused()
            self.page.keyboard.press("Enter")
            self.assert_page(name, title)

    def test_failed_refresh_preserves_data_and_recovers(self):
        self.page.goto(self.base)
        expect(self.page.locator("#stats-badge")).to_have_text("live")
        for mode in ["http-error", "offline", "invalid-json"]:
            with self.subTest(mode=mode):
                self.mode = mode
                self.page.locator("#btn-refresh").click()
                expect(self.page.locator("#stats-badge")).to_have_text("Data lama")
                expect(self.page.locator("#stats-status")).to_contain_text("Data terakhir:")
                expect(self.page.locator("#st-total")).to_have_text("7")
                expect(self.page.locator("#connection-state")).not_to_contain_text("live")
                expect(self.page.locator("#btn-refresh")).to_be_enabled()
                self.mode = "ok"
                self.page.locator("#btn-refresh").click()
                expect(self.page.locator("#stats-badge")).to_have_text("live")
                expect(self.page.locator("#stats-status")).not_to_have_class("stats-error")

    def test_initial_failure_and_automatic_recovery(self):
        self.mode = "offline"
        self.page.clock.install()
        self.page.goto(self.base)
        expect(self.page.locator("#stats-badge")).to_have_text("Tidak tersedia")
        expect(self.page.locator("#stats-status")).to_contain_text("Data belum tersedia.")
        self.mode = "ok"
        self.page.clock.fast_forward(5000)
        expect(self.page.locator("#stats-badge")).to_have_text("live")

    def test_hung_request_times_out_and_can_retry(self):
        self.page.add_init_script("""
            const originalFetch = window.fetch;
            window.fetch = (url, options) => url === '/solver/stats'
                ? new Promise((resolve, reject) => options.signal.addEventListener(
                    'abort', () => reject(new DOMException('Timeout', 'AbortError'))))
                : originalFetch(url, options);
        """)
        self.page.clock.install()
        self.page.goto(self.base)
        expect(self.page.locator("#btn-refresh")).to_be_disabled()
        self.page.clock.fast_forward(10000)
        expect(self.page.locator("#stats-badge")).to_have_text("Tidak tersedia")
        expect(self.page.locator("#btn-refresh")).to_be_enabled()


if __name__ == "__main__":
    unittest.main()
