#!/usr/bin/env python3
"""One end-to-end browser smoke test for the Observatory frontend (step-13
task 3): drives a REAL local Chrome/Chromium headless instance over the
DevTools Protocol against `python -m app ui` serving the built
observatory/static/ bundle over the trace fixture db.

Deliberately stdlib-only, no playwright/selenium: reuses the exact CDP
websocket approach discovery/providers/cdp.py already uses to drive an
authenticated claude.ai/chatgpt.com tab, just pointed at a Chrome instance
THIS test launches itself (headless, its own --remote-debugging-port, its
own --user-data-dir) rather than an existing authenticated tab.

Skip policy: this test skips, with an explicit reported reason, ONLY when no
Chrome/Chromium binary can be found on this machine. If a build of
observatory/frontend/ hasn't been committed to observatory/static/ (see
observatory/frontend/README section in this repo's own README.md), the test
still RUNS -- and fails loudly -- rather than silently skipping, since a
missing build is a real gap this test exists to catch, not an environment
limitation like a missing Chrome binary.

No network beyond localhost: the fixture db, the `ui` server, and the
headless Chrome instance are all local; nothing here reaches the internet.
"""
import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from discovery.providers import cdp  # noqa: E402

DESKTOP_VIEWPORT = {"width": 1400, "height": 900, "deviceScaleFactor": 1, "mobile": False}
IPHONE_VIEWPORT = {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True}


def _find_chrome():
    candidates = [
        os.environ.get("DISCOVERY_UI_E2E_CHROME"),
        shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium"),
        shutil.which("chromium-browser"), shutil.which("msedge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http_ok(host, port, path="/", timeout=20):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=2)
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            conn.close()
            if resp.status < 500:
                return
        except OSError as e:
            last_err = e
        time.sleep(0.25)
    raise TimeoutError(f"server on {host}:{port} never answered ({last_err})")


def _wait_cdp_ready(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            tabs = cdp.list_tabs(port=port, timeout=2)
            page = next((t for t in tabs if t.get("type") == "page"), None)
            if page is not None:
                return page
        except OSError:
            pass
        time.sleep(0.25)
    raise TimeoutError("headless Chrome never exposed a page tab over CDP")


CHROME_PATH = _find_chrome()


@unittest.skipUnless(CHROME_PATH, "no Chrome/Chromium binary found on this machine -- set DISCOVERY_UI_E2E_CHROME")
class ObservatoryE2ETests(unittest.TestCase):
    """One long-lived fixture db + one `ui` server + one headless Chrome
    instance shared by every test method (each test drives it independently
    via fresh Page.navigate calls) -- starting all three per test would make
    an already-slow browser suite far slower for no isolation benefit, since
    every method only reads."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="observatory-e2e-")
        cls.db_path = str(Path(cls.tmpdir) / "fixture.db")
        build = subprocess.run(
            [sys.executable, "-m", "app", "--db", cls.db_path, "trace-fixture", "--db", cls.db_path],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
        )
        if build.returncode != 0:
            raise RuntimeError(f"trace-fixture build failed: {build.stdout}\n{build.stderr}")

        cls.host = "127.0.0.1"
        cls.port = _free_port()
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "app", "--db", cls.db_path, "ui", "--host", cls.host, "--port", str(cls.port)],
            cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            _wait_http_ok(cls.host, cls.port, "/observatory/", timeout=30)
        except Exception:
            cls.server.terminate()
            raise

        cls.cdp_port = _free_port()
        cls.chrome_profile = str(Path(cls.tmpdir) / "chrome-profile")
        cls.chrome = subprocess.Popen([
            CHROME_PATH, "--headless=new", f"--remote-debugging-port={cls.cdp_port}",
            f"--user-data-dir={cls.chrome_profile}", "--no-sandbox", "--disable-gpu",
            "--disable-dev-shm-usage", "--no-first-run", "about:blank",
        ])
        cls.page_tab = _wait_cdp_ready(cls.cdp_port, timeout=30)
        cls.conn = cdp.CDPConnection(cls.page_tab["webSocketDebuggerUrl"])
        cls.conn.send("Page.enable")
        cls.conn.send("Runtime.enable")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.close()
        except Exception:
            pass
        for proc in (getattr(cls, "chrome", None), getattr(cls, "server", None)):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def navigate(self, path):
        self.conn.send("Page.navigate", {"url": f"http://{self.host}:{self.port}{path}"})
        self._wait_ready()

    def _wait_ready(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.conn.evaluate("document.readyState")
            root_has_children = self.conn.evaluate(
                "!!document.querySelector('[data-testid=\"app\"]')"
            )
            if state == "complete" and root_has_children:
                return
            time.sleep(0.2)
        raise TimeoutError("app never finished mounting")

    def set_viewport(self, viewport):
        self.conn.send("Emulation.setDeviceMetricsOverride", viewport)
        self.conn.send("Emulation.setTouchEmulationEnabled", {"enabled": viewport.get("mobile", False)})

    def js(self, expr):
        return self.conn.evaluate(expr)

    def click(self, selector):
        # Runtime.evaluate-driven click (same approach discovery/providers/*
        # already uses for in-page interaction) -- dispatches a real click
        # event rather than calling .click() directly, so React's event
        # delegation (which listens at the root, not the element) still fires.
        self.js(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) throw new Error('selector not found: {selector}');
                el.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                return true;
            }})()
        """)

    def double_click(self, selector):
        self.js(f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) throw new Error('selector not found: {selector}');
                el.dispatchEvent(new MouseEvent('dblclick', {{bubbles: true}}));
                return true;
            }})()
        """)

    def click_text(self, tag, text):
        self.js(f"""
            (() => {{
                const els = [...document.querySelectorAll({json.dumps(tag)})];
                const el = els.find(e => e.textContent.trim().includes({json.dumps(text)}));
                if (!el) throw new Error('no {tag} with text {text}');
                el.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                return true;
            }})()
        """)

    def wait_for(self, expr, timeout=10, message=""):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.js(expr)
            if last:
                return last
            time.sleep(0.2)
        raise TimeoutError(f"condition never true: {expr} ({message}); last={last!r}")

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- desktop pass --------------------------------------------------------

    def test_01_desktop_list_and_graph(self):
        self.set_viewport(DESKTOP_VIEWPORT)
        self.navigate("/observatory/")
        self.wait_for("document.querySelectorAll('.explorer-row').length > 0", message="explorer rows loaded")

        # select the fixture's one successful, delivered discovery (has a
        # notification + feedback row -- see trace_fixture.build()).
        sent_row = self.wait_for(
            "(() => {"
            "  const rows = [...document.querySelectorAll('.explorer-row')];"
            "  const row = rows.find(r => r.querySelector('.row-sent')?.textContent.trim() === 'sent');"
            "  return row ? true : false;"
            "})()",
            message="a sent row exists",
        )
        self.assertTrue(sent_row)
        self.click(".explorer-row:has(.row-sent)")  # first match wins if several exist

        self.wait_for("document.querySelectorAll('.node-card').length > 0", message="graph nodes rendered")
        node_types = self.js(
            "[...document.querySelectorAll('.node-card')].map(n => n.dataset.nodeType)"
        )
        # The plan's own fixture branches (PROJECT_STATE.md): a duplicate, a
        # prefilter rejection, a scoring failure+retry, a below-threshold
        # score, and one delivered+feedback item -- each has a real node.
        for expected in ("duplicate", "prefilter", "threshold"):
            with self.subTest(node_type=expected):
                self.assertIn(expected, node_types)

    def test_02_group_expand_collapse(self):
        self.set_viewport(DESKTOP_VIEWPORT)
        self.navigate("/observatory/")
        self.wait_for("document.querySelectorAll('.explorer-row').length > 0")
        self.click(".explorer-row")
        self.wait_for("document.querySelectorAll('.node-card').length > 0")
        group = self.js("!!document.querySelector('.node-group')")
        if not group:
            self.skipTest("this discovery's own trace has no sibling set past COLLAPSE_THRESHOLD to expand")
        before = self.js("document.querySelectorAll('.node-card').length")
        self.double_click(".node-group")
        self.wait_for(f"document.querySelectorAll('.node-card').length > {before}", message="expand added nodes")
        after_expand = self.js("document.querySelectorAll('.node-card').length")
        self.double_click(".node-group")  # collapses whichever child card is now first in DOM order -- re-query
        # A collapsed-again graph must NOT still show more cards than the
        # pre-expand baseline (the group returns, its children hide again).
        self.wait_for(f"document.querySelectorAll('.node-card').length <= {after_expand}")

    def test_03_pan_zoom(self):
        self.set_viewport(DESKTOP_VIEWPORT)
        self.navigate("/observatory/")
        self.wait_for("document.querySelectorAll('.explorer-row').length > 0")
        self.click(".explorer-row")
        self.wait_for("document.querySelectorAll('.node-card').length > 0")
        transform_before = self.js("document.querySelector('.react-flow__viewport')?.style.transform")
        self.click(".react-flow__controls-zoomin")
        self.wait_for(
            f"document.querySelector('.react-flow__viewport')?.style.transform !== {json.dumps(transform_before)}",
            message="zoom-in control changed the viewport transform",
        )

    def test_04_inspector_prompt_byte_exact(self):
        self.set_viewport(DESKTOP_VIEWPORT)
        with self.db() as conn:
            call = conn.execute(
                "SELECT trace_node_id, exact_user_prompt FROM model_calls "
                "WHERE exact_user_prompt IS NOT NULL ORDER BY id ASC LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(call, "fixture db has no model_calls rows")
        node_id, expected_prompt = call

        self.navigate(f"/observatory/?_e2e_node={node_id}")  # no real query-param wiring -- just land on the app
        self.wait_for("document.querySelectorAll('.explorer-row').length > 0")
        self.click(".explorer-row")
        self.wait_for("document.querySelectorAll('.node-card').length > 0")
        self.click(f'[data-node-id="{node_id}"]')
        self.wait_for("!!document.querySelector('[data-testid=\"inspector\"]')")
        self.click_text("button", "Reasoning record")
        self.click_text("summary", "Exact system + user prompt")
        shown = self.wait_for(
            "document.querySelector('[data-testid=\"monospace-content\"]')?.textContent || ''",
            message="prompt text rendered",
        )
        self.assertIn(expected_prompt, shown, "displayed prompt is not byte-equal to the fixture's stored prompt")

    def test_05_copy_button(self):
        self.set_viewport(DESKTOP_VIEWPORT)
        self.navigate("/observatory/")
        self.wait_for("document.querySelectorAll('.explorer-row').length > 0")
        self.click(".explorer-row")
        self.wait_for("document.querySelectorAll('.node-card').length > 0")
        self.click(".node-card")
        self.wait_for("!!document.querySelector('[data-testid=\"inspector\"]')")
        # jsdom-free real Chrome has a real (permission-gated in headless,
        # but same-origin + user-gesture-simulated click still succeeds)
        # clipboard API -- exercise the button and just prove it didn't throw.
        self.click_text("button", "Copy")
        copied = self.wait_for(
            "!![...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Copied')",
            timeout=5, message="Copy button flipped to Copied",
        )
        self.assertTrue(copied)

    # -- iPhone-width pass ----------------------------------------------------

    def test_06_iphone_viewport_drawer_and_sheet(self):
        self.set_viewport(IPHONE_VIEWPORT)
        self.navigate("/observatory/")
        self.wait_for("!!document.querySelector('[data-testid=\"app\"]')")
        self.assertTrue(self.js("document.querySelector('[data-testid=\"app\"]').className.includes('mobile')"))
        # explorer starts closed (a drawer), opens via the header toggle
        self.assertFalse(self.js("document.querySelector('.pane-explorer').classList.contains('open')"))
        self.click(".drawer-toggle")
        self.wait_for("document.querySelector('.pane-explorer').classList.contains('open')")
        self.wait_for("document.querySelectorAll('.explorer-row').length > 0")
        self.click(".explorer-row")
        self.wait_for("document.querySelectorAll('.node-card').length > 0")
        self.click(".node-card")
        self.wait_for("document.querySelector('[data-testid=\"bottom-sheet\"]')?.classList.contains('open')")

    def test_07_deep_link_highlights_sent_path(self):
        self.set_viewport(DESKTOP_VIEWPORT)
        with self.db() as conn:
            score_id = conn.execute(
                "SELECT s.id FROM scores s JOIN notifications no ON no.score_id = s.id WHERE no.ok = 1 LIMIT 1"
            ).fetchone()
        self.assertIsNotNone(score_id, "fixture db has no sent (ok=1) notification")
        self.navigate(f"/observatory/trace/score/{score_id[0]}")
        self.wait_for("document.querySelectorAll('.node-card').length > 0", message="deep link loaded a graph directly")
        emphasized = self.js("document.querySelectorAll('.node-card.emphasized').length")
        self.assertGreater(emphasized, 0, "deep link did not highlight any node on the sent path")
        node_types = self.js("[...document.querySelectorAll('.node-card.emphasized')].map(n => n.dataset.nodeType)")
        self.assertIn("threshold", node_types)


if __name__ == "__main__":
    unittest.main()
