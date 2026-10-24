"""End-to-end tests of the Book Sale app in a real (headless Chromium) browser: first load, offline reloads,
updates, and a slow / broken / damaged connection. Needs: pip install playwright (browser already present)."""
import hashlib, json, os, shutil, tempfile, unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from dev_server import Server

HERE = Path(__file__).parent


def make_items(n=400):
    """A small made-up library, so the tests need nothing from your real data."""
    adj = ["Silent", "Golden", "Hidden", "Crimson", "Forgotten", "Winter", "Distant", "Broken"]
    noun = ["Garden", "Harvest", "Kingdom", "River", "Lantern", "Orchard", "Compass", "Tide"]
    out = []
    for k in range(n):
        out.append({"id": k + 1, "t": f"The {adj[k % 8]} {noun[(k // 8) % 8]} {k}", "s": "", "a": f"Author{k % 37}, Test",
                    "m": "book" if k % 25 else "dvd", "r": "", "i": f"9780000{k:06d}",
                    "b": ["hardcover", "paperback", "board book", ""][k % 4], "g": ["test"], "l": ["Elm Street"]})
    return out


def publish(data_dir: Path, items, when="2026-10-20T06:00:00Z"):
    """Write a snapshot + version.json exactly as the exporter would, for the given items."""
    body = json.dumps({"format": 1, "count": len(items), "items": items}, ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")
    sha = hashlib.sha256(body).hexdigest()
    name = f"snapshot.{sha[:12]}.json"
    (data_dir / name).write_bytes(body)
    (data_dir / "version.json").write_text(json.dumps(
        {"format": 1, "generated_at": when, "count": len(items), "sha256": sha, "file": name, "bytes": len(body)}))
    return name, body


class Pwa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.items = make_items()
        publish(self.data, self.items)
        self.n = len(self.items)
        self.server = Server(self.data).start()
        self.ctx = None
        self.console = []

    def tearDown(self):
        bad = [m for m in self.console if "Content Security Policy" in m or "Refused to" in m]
        if self.ctx:
            self.ctx.close()
        self.server.stop()
        shutil.rmtree(self.tmp)
        self.assertEqual(bad, [], "the app must obey the strict Content-Security-Policy nginx will send")

    # ---------------------------------------------------------------- helpers
    def open(self, config=None, ctx=None):
        self.ctx = ctx or self.browser.new_context()
        if config:
            self.ctx.add_init_script(f"window.STORE_CONFIG = {json.dumps(config)};")
        page = self.ctx.new_page()
        page.on("console", lambda m: self.console.append(m.text))
        page.goto(self.server.url)
        return page

    def reopen(self, page, config=None):
        page.close()
        page = self.ctx.new_page()
        page.on("console", lambda m: self.console.append(m.text))
        page.goto(self.server.url)
        return page

    def pill(self, page):
        return page.locator("#pill").get_attribute("data-state")

    def wait_state(self, page, state, timeout=15000):
        page.wait_for_function("(s) => document.getElementById('pill').getAttribute('data-state') === s", arg=state, timeout=timeout)

    def msg(self, page):
        return page.locator("#pMsg").text_content()

    def sw_ready(self, page):
        page.evaluate("navigator.serviceWorker.ready.then(() => true)")
        page.wait_for_timeout(300)

    def a_title(self):
        return next(i["t"] for i in self.items if i["m"] == "book" and i["id"] == 77)

    # ------------------------------------------------------------------ tests
    def test_first_open_downloads_then_shows_count_and_date_and_searches(self):
        page = self.open()
        self.wait_state(page, "ready")
        text = page.locator("#pillText").text_content()
        self.assertIn(f"{self.n:,} titles", text)
        self.assertRegex(text, r"synced \w+ \d+")
        title = self.a_title()
        page.fill("#q", title[:9].lower())
        page.wait_for_selector(".row")
        self.assertGreater(page.locator(".row").count(), 0)
        page.fill("#q", title.replace(title.split()[0], title.split()[0][:-1] + title.split()[0][-1]))  # typo-free sanity
        self.assertIn(title.split()[0].lower(), page.locator(".row .t").first.text_content().lower())

    def test_typo_and_partial_matches_still_find_the_book(self):
        page = self.open(); self.wait_state(page, "ready")
        w = self.a_title().split()[1]
        typo = w[:2] + w[3] + w[2] + w[4:]            # swap two letters
        page.fill("#q", typo)
        page.wait_for_selector(".row")
        self.assertGreater(page.locator(".row").count(), 0)

    def test_books_with_no_binding_show_no_badge_and_no_placeholder(self):
        page = self.open(); self.wait_state(page, "ready")
        # k % 4 == 3 means no binding: k=3 is "The Crimson Garden 3"; k=4 is hardcover
        page.fill("#q", "crimson garden 3")
        page.wait_for_selector(".row")
        row = page.locator(".row", has_text="The Crimson Garden 3").first
        self.assertEqual(row.locator(".badge").count(), 0)
        self.assertIn("nobadge", row.get_attribute("class"))
        row.click()
        self.assertNotIn("Binding", page.locator(".row .detail").first.text_content())
        self.assertNotIn("BINDING ?", page.locator("body").text_content())
        # a book that does have one still shows it
        page.fill("#q", "forgotten garden 4")
        page.wait_for_selector(".row")
        self.assertEqual(page.locator(".row", has_text="The Forgotten Garden 4").first.locator(".badge").text_content(), "HARDCOVER")

    def test_offline_reload_still_searches_from_the_saved_copy(self):
        page = self.open(); self.wait_state(page, "ready"); self.sw_ready(page)
        self.ctx.set_offline(True)
        page = self.reopen(page)
        self.wait_state(page, "ready")                     # offline: not an error state, the saved copy is fine
        self.assertFalse(page.locator("#q").is_disabled())
        page.fill("#q", self.a_title()[:9])
        page.wait_for_selector(".row")
        self.assertIn(f"{self.n:,} titles", page.locator("#pillText").text_content())
        page.click("#pill")
        self.assertIn("Using the copy saved on this phone", self.msg(page))

    def test_second_open_with_no_change_does_not_download_the_library_again(self):
        page = self.open(); self.wait_state(page, "ready")
        page = self.reopen(page); self.wait_state(page, "ready")
        page.wait_for_timeout(300)
        snaps = [h for h in self.server.hits if "/data/snapshot." in h]
        self.assertEqual(len(snaps), 1)
        self.assertGreaterEqual(len([h for h in self.server.hits if h == "/data/version.json"]), 2)

    def test_a_new_library_copy_replaces_the_old_one_and_updates_the_count(self):
        page = self.open(); self.wait_state(page, "ready")
        extra = [{"id": 900000 + k, "t": f"Brand New Book {k}", "s": "", "a": "New, Author", "m": "book", "r": "",
                  "i": f"97800000{k:05d}", "b": "hardcover", "g": [], "l": []} for k in range(5)]
        publish(self.data, self.items + extra, when="2026-10-21T06:00:00Z")
        page = self.reopen(page)
        page.wait_for_function("(s) => document.getElementById('pillText').textContent.includes(s)", arg=f"{self.n + 5:,} titles", timeout=15000)
        page.fill("#q", "brand new book")
        page.wait_for_selector(".row")
        self.assertEqual(page.locator(".row").count(), 5)
        # and it is the saved copy now: offline reload keeps the new count
        self.sw_ready(page); self.ctx.set_offline(True)
        page = self.reopen(page); self.wait_state(page, "ready")
        self.assertIn(f"{self.n + 5:,} titles", page.locator("#pillText").text_content())

    def test_a_stalled_version_check_never_freezes_search(self):
        page = self.open(); self.wait_state(page, "ready")
        self.server.faults.update({"version": "stall", "stall_seconds": 3})
        page.close(); page = self.ctx.new_page()
        self.ctx.add_init_script("window.STORE_CONFIG = {versionTimeoutMs: 800};")
        page = self.ctx.new_page(); page.goto(self.server.url)
        self.wait_state(page, "syncing", 5000)
        page.fill("#q", self.a_title()[:9])                  # typed while the check is still hanging
        page.wait_for_selector(".row", timeout=1500)
        self.assertEqual(self.pill(page), "syncing")
        self.wait_state(page, "ready", 8000)
        page.click("#pill")
        self.assertIn("did not answer in time", self.msg(page))
        self.assertIn(f"{self.n:,} titles", page.locator("#pillText").text_content())

    def test_a_stalled_download_is_abandoned_and_the_old_copy_stays(self):
        page = self.open(); self.wait_state(page, "ready")
        publish(self.data, self.items[:-3] + [dict(self.items[-1], id=999999, t="Different")], when="2026-10-21T06:00:00Z")
        self.server.faults.update({"snapshot": "stall", "stall_seconds": 3})
        self.ctx.add_init_script("window.STORE_CONFIG = {snapshotTimeoutMs: 800};")
        page.close(); page = self.ctx.new_page(); page.goto(self.server.url)
        self.wait_state(page, "ready", 10000)
        page.click("#pill")
        self.assertIn("did not answer in time", self.msg(page))
        self.assertIn(f"{self.n:,} titles", page.locator("#pillText").text_content())
        page.fill("#q", self.a_title()[:9]); page.wait_for_selector(".row")

    def test_truncated_damaged_and_shrunken_downloads_are_rejected(self):
        page = self.open(); self.wait_state(page, "ready")
        cases = []
        # 1) truncated in transit
        publish(self.data, self.items + [dict(self.items[0], id=800001)], when="2026-10-21T06:00:00Z")
        self.server.faults.update({"snapshot": "truncate"})
        cases.append(("cut short", None))
        # 2) same size, one byte flipped (checksum must catch it)
        # 3) far fewer titles
        for expect, prep in [("cut short", lambda: None),
                             ("checksum", lambda: self._flip_a_byte()),
                             ("far fewer", lambda: self._shrink())]:
            self.server.faults.clear()
            if expect == "cut short":
                self.server.faults["snapshot"] = "truncate"
            prep()
            page = self.reopen(page)
            self.wait_state(page, "ready", 10000)
            page.click("#pill")
            self.assertIn(expect, self.msg(page), expect)
            self.assertIn(f"{self.n:,} titles", page.locator("#pillText").text_content(), expect)
            page.click("#pill")

    def _flip_a_byte(self):
        v = json.loads((self.data / "version.json").read_text())
        f = self.data / v["file"]
        b = bytearray(f.read_bytes()); i = b.index(b'"t":"') + 6
        b[i] = ord("Z") if b[i] != ord("Z") else ord("Y")
        f.write_bytes(bytes(b))

    def _shrink(self):
        publish(self.data, self.items[: self.n // 3], when="2026-10-22T06:00:00Z")

    def test_server_error_keeps_the_saved_copy(self):
        page = self.open(); self.wait_state(page, "ready")
        self.server.faults["version"] = "500"
        page = self.reopen(page); self.wait_state(page, "ready")
        page.click("#pill")
        self.assertIn("error (500)", self.msg(page))
        self.assertFalse(page.locator("#q").is_disabled())

    def test_first_ever_open_with_no_connection_says_so_and_does_not_pretend(self):
        self.ctx = self.browser.new_context()
        self.ctx.set_offline(True)
        page = self.ctx.new_page()
        self.server_url_offline(page)
        self.wait_state(page, "empty")
        self.assertTrue(page.locator("#q").is_disabled())
        self.assertIn("No library data", page.locator("#pillText").text_content())
        self.assertIn("Connect to your home network", page.locator("#results").text_content())

    def server_url_offline(self, page):
        # set_offline blocks even localhost, so load the page first, then go offline before it syncs
        self.ctx.set_offline(False)
        self.server.faults["version"] = "500"
        page.goto(self.server.url)
        self.ctx.set_offline(True)

    def test_recheck_when_the_connection_returns(self):
        self.server.faults["version"] = "500"
        page = self.open()
        self.wait_state(page, "empty")
        self.server.faults.clear()
        page.evaluate("window.dispatchEvent(new Event('online'))")
        self.wait_state(page, "ready")
        self.assertIn(f"{self.n:,} titles", page.locator("#pillText").text_content())


if __name__ == "__main__":
    unittest.main()
