"""
The DevTools pipe driver: its pure parts always, and the whole thing
against the real Chromium on this machine when there is one.
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse

import pytest

from bot.cdp import (BrowserGone, CdpBrowser, PageError, evaluate_source_is_function,
                     find_chromium, headless_flag, split_frames)

PNG = b"\x89PNG\r\n\x1a\n"


def test_find_chromium_prefers_the_headless_shell_and_the_newest(tmp_path):
    for rel in ("chromium_headless_shell-1140/chrome-linux/headless_shell",
                "chromium-1140/chrome-linux/chrome",
                "chromium_headless_shell-1194/chrome-linux/headless_shell",
                "chromium-1300/chrome-linux/chrome"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    env = {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path)}
    assert find_chromium(env).endswith("chromium_headless_shell-1194/chrome-linux/headless_shell")
    explicit = tmp_path / "chromium-1140/chrome-linux/chrome"
    assert find_chromium({"BOT_CHROMIUM_PATH": str(explicit)}) == str(explicit)
    assert find_chromium({"BOT_CHROMIUM_PATH": str(tmp_path / "missing")}) is None


def test_headless_flag_is_the_lean_one_everywhere():
    assert headless_flag("/x/chromium_headless_shell-1140/chrome-linux/headless_shell") == "--headless"
    assert headless_flag("/x/chromium-1140/chrome-linux/chrome") == "--headless"


def test_pipe_framing():
    frames, rest = split_frames(b'{"id":1}\0{"method":"x"}\0{"partial')
    assert frames == [b'{"id":1}', b'{"method":"x"}']
    assert rest == b'{"partial'
    assert split_frames(b"") == ([], b"")


def test_function_sources_are_told_from_expressions():
    for src in ("async (cfg) => { await window.zoomJoin(cfg); }", "() => { return 1; }",
                "(cfg) => cfg", "async ([text, to]) => await f(text, to)", "function f() {}",
                "async () => await window.zoomListUsers()"):
        assert evaluate_source_is_function(src), src
    for src in ("typeof window.ZoomMtgEmbedded !== 'undefined'", "1 + 1", "window.ready === 1"):
        assert not evaluate_source_is_function(src), src


CHROMIUM = find_chromium()
needs_chromium = pytest.mark.skipif(not CHROMIUM, reason="no Chromium on this machine")


def data_url(html: str) -> str:
    return "data:text/html," + urllib.parse.quote(html)


PAGE = """<html><body><div id="box" style="width:120px;height:80px;background:#0a5">hi</div>
<script>
window.ready = 1;
console.error('boom-console');
setTimeout(() => { window.onZoomLifecycle({ type: 'ping', detail: 'pong' }); }, 30);
setTimeout(() => { throw new Error('boom-pageerror'); }, 40);
</script></body></html>"""


@needs_chromium
def test_real_chromium_roundtrip():
    async def run():
        browser = await CdpBrowser(CHROMIUM, [
            "--no-sandbox", "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream"]).launch()
        try:
            page = await browser.new_page()
            got, consoles, errors, failed = [], [], [], []
            page.on("console", lambda m: consoles.append((m.type, m.text)))
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("requestfailed", lambda r: failed.append((r.url, r.failure)))

            async def on_lifecycle(payload):
                got.append(payload)

            await page.expose_function("onZoomLifecycle", on_lifecycle)
            await page.goto(data_url(PAGE), wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_function("window.ready === 1", timeout=5000)

            # Playwright's evaluate, both shapes, with an argument.
            assert await page.evaluate("async (x) => x.a + 1", {"a": 41}) == 42
            assert await page.evaluate("async ([a, b]) => a + b", [2, 3]) == 5
            assert await page.evaluate("1 + 1") == 2
            with pytest.raises(PageError) as ei:
                await page.evaluate("() => { throw new Error('nope'); }")
            assert "nope" in str(ei.value)

            # The page's own words reach Python: a bridge call, a thrown
            # error, a console error.
            for _ in range(80):
                if got and errors:
                    break
                await asyncio.sleep(0.05)
            assert got == [{"type": "ping", "detail": "pong"}]
            assert any("boom-pageerror" in e for e in errors)
            assert ("error", "boom-console") in consoles

            # Pictures: the whole page and one element.
            png = await page.screenshot(type="png", timeout=10000)
            assert png[:8] == PNG
            clip = await page.locator("#box").screenshot(type="png", timeout=4000, animations="disabled")
            assert clip[:8] == PNG
            with pytest.raises(RuntimeError):
                await page.locator("#nothing").screenshot()

            # The Network domain stays off: a failed request is not
            # reported, and no network frame ever reaches this process.
            await page.evaluate(
                "async () => { try { await fetch('http://127.0.0.1:9/nothing'); } catch (e) {} }")
            await asyncio.sleep(0.3)
            assert failed == []

            profile = browser._profile_dir
            proc = browser._proc
            gone = []
            browser.on("disconnected", lambda: gone.append(True))
            await browser.close()
            assert browser.closed
            assert proc.returncode is not None
            assert not os.path.isdir(profile)
            # A deliberate close is not a death.
            await asyncio.sleep(0.1)
            assert not gone
            with pytest.raises(BrowserGone):
                await page.evaluate("1")
        finally:
            await browser.close()

    asyncio.run(run())


@needs_chromium
@pytest.mark.parametrize("wiring", ["bash", "hook"])
def test_both_pipe_wirings_reach_the_browser(wiring):
    async def run():
        browser = await CdpBrowser(CHROMIUM, ["--no-sandbox"], wiring=wiring).launch()
        try:
            page = await browser.new_page()
            assert await page.evaluate("2 * 21") == 42
        finally:
            await browser.close()
        assert browser._proc.returncode is not None

    asyncio.run(run())


@needs_chromium
def test_real_chromium_death_is_reported():
    async def run():
        browser = await CdpBrowser(CHROMIUM, ["--no-sandbox"]).launch()
        page = await browser.new_page()
        gone = []
        browser.on("disconnected", lambda: gone.append(True))
        browser._proc.kill()
        for _ in range(100):
            if gone:
                break
            await asyncio.sleep(0.05)
        assert gone == [True]
        with pytest.raises(BrowserGone):
            await page.evaluate("1")
        await browser.close()

    asyncio.run(run())


@needs_chromium
def test_cdp_client_opens_the_zoom_page(monkeypatch):
    """The meeting client's own page-opening path on the direct driver,
    with the lookout diet flags: the camera picture flag, the bridge, the
    SDK-global wait, diagnostics, and a clean close."""
    from bot.meeting_client import CdpZoomClient, _looks_like_browser_death

    monkeypatch.setenv("BOT_CAMERA_FACE", "on")
    html = ("<html><body><script>window.ZoomMtgEmbedded = { createClient() { return {}; } };"
            "</script></body></html>")
    c = CdpZoomClient(page_url=data_url(html), headless=True)

    async def run():
        await c._driver_start()
        args = c._launch_args(lookout=True)
        assert any(a.startswith("--use-file-for-fake-video-capture=") for a in args)
        assert "--single-process" in args
        await c._open_page(args)
        assert await c._page.evaluate("() => typeof window.ZoomMtgEmbedded") == "object"
        d = await c.diagnostics()
        assert "diagnostics failed" in d.get("error", "")
        assert d["dietActive"] is False  # only join() marks the diet
        await c.close()
        assert c._page is None and c._browser is None

    asyncio.run(run())
    assert _looks_like_browser_death(BrowserGone("browser has been closed (the browser process died)"))
    assert not _looks_like_browser_death(PageError("Joining meeting timeout"))


def test_the_default_driver_is_the_relay_until_the_direct_one_is_proven(monkeypatch):
    from bot.meeting_client import (CdpZoomClient, PlaywrightZoomClient, browser_driver_name,
                                    build_meeting_client)
    monkeypatch.delenv("BOT_BROWSER_DRIVER", raising=False)
    assert browser_driver_name() == "playwright"
    assert isinstance(build_meeting_client("http://x", True), PlaywrightZoomClient)
    monkeypatch.setenv("BOT_BROWSER_DRIVER", "cdp")
    assert browser_driver_name() == "cdp"
    assert isinstance(build_meeting_client("http://x", True), CdpZoomClient)


def _manager_app(client_factory):
    from fastapi.testclient import TestClient
    from bot.app import build_app
    from bot.config import Config
    from bot.storage import NullStorage
    from bot.tests.test_bot import FakeBackend

    cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                 sdk_key="KEY", sdk_secret="SECRET",
                 public_base_url="http://bot", headless=True, drive_folder_id=None)
    backend = FakeBackend()
    app = build_app(cfg, backend=backend, client_factory=client_factory,
                    storage_factory=lambda s, f: NullStorage())
    return TestClient(app), backend


def test_a_join_that_dies_on_the_direct_driver_is_retried_on_playwright(monkeypatch):
    """The browser dying under the new driver costs one retry, not the
    class. A Zoom refusal, which would fail the same on any driver, is
    not retried."""
    from bot.meeting_client import FakeMeetingClient
    import bot.meeting_client as mc

    closed, fallback_joins = [], []

    class DirectDies(FakeMeetingClient):
        DRIVER = "cdp"

        async def join(self, **kwargs):
            raise BrowserGone("browser has been closed (the browser process died (exit code -5))")

        async def close(self):
            closed.append("direct")

    class FallbackClient(FakeMeetingClient):
        DRIVER = "playwright"

        def __init__(self, page_url, headless):
            super().__init__()
            fallback_joins.append(page_url)

        async def join(self, **kwargs):
            self.joined = True
            fallback_joins.append(kwargs["meeting_number"])

    monkeypatch.setattr(mc, "PlaywrightZoomClient", FallbackClient)
    monkeypatch.delenv("BOT_DRIVER_FALLBACK", raising=False)
    c, backend = _manager_app(lambda page_url, headless: DirectDies())
    r = c.post("/bots", json={"meeting_id": "98765", "session_ref": "7",
                              "display_name": "AALB Assistant"})
    assert r.status_code == 200, r.text
    assert closed == ["direct"]
    assert fallback_joins[-1] == "98765"
    assert not [e for e in backend.events if e["type"] == "error"]

    # Zoom saying no is not a driver failure.
    fallback_joins.clear()

    class DirectRefused(FakeMeetingClient):
        DRIVER = "cdp"

        async def join(self, **kwargs):
            raise PageError("Joining meeting timeout")

    c2, backend2 = _manager_app(lambda page_url, headless: DirectRefused())
    r2 = c2.post("/bots", json={"meeting_id": "98765", "session_ref": "8",
                                "display_name": "AALB Assistant"})
    assert r2.status_code >= 400
    assert not fallback_joins
    assert [e for e in backend2.events if e["type"] == "error"]

    # And the switch to turn the retry off.
    monkeypatch.setenv("BOT_DRIVER_FALLBACK", "off")
    c3, _ = _manager_app(lambda page_url, headless: DirectDies())
    r3 = c3.post("/bots", json={"meeting_id": "98765", "session_ref": "9",
                                "display_name": "AALB Assistant"})
    assert r3.status_code >= 400
    assert not fallback_joins
