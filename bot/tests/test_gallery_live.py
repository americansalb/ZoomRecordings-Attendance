"""
The gallery's page turning, in a real Chromium, with the production
static/zoom_client.js.

The fixture page lays tiles out by the Zoom Meeting SDK 6.2.0 gallery's own
rules, read out of the kit: raised hands first, then cameras on, the bot's
own tile second; two rows of two at the bot's 640 by 365; the last page
back-filled from the one before; and the page buttons as Zoom draws them,
MUI buttons labelled "Decrease page" and "Increase page" inside a div
labelled "Pagination", the one that cannot be used hidden with
visibility:hidden, and no buttons at all while everybody fits on one page.

FOUND 2026-09-25: the bot looked for "next page" and "previous page", so on
this very page it could never turn, and the three students on the first page
got every face check. The first test is that fault, pinned.

Run:  python -m pytest bot/tests/test_gallery_live.py   (needs a Chromium)
"""

from __future__ import annotations

import asyncio
import http.server
import re
import socketserver
import threading
import urllib.parse
from pathlib import Path

import pytest

from bot.cdp import CdpBrowser, find_chromium

STATIC = Path(__file__).resolve().parent.parent / "static"
CHROMIUM = find_chromium()
needs_chromium = pytest.mark.skipif(not CHROMIUM, reason="no Chromium on this machine")

FIXTURE = """<!doctype html>
<html><head><meta charset="utf-8"><title>zoom gallery fixture</title>
<style>
  body { margin:0; background:#111; }
  #gallery { position:relative; width:640px; height:365px; }
  #tiles { position:absolute; inset:0; display:grid; grid-template-columns:316px 316px;
           grid-template-rows:176px 176px; gap:4px; }
  .avatar { background:#333; color:#ddd; font:12px sans-serif; }
</style></head>
<body>
  <div id="zoom-root"><div id="gallery"><div id="tiles"></div></div></div>
  <script>
    const url = new URL(location.href);
    const n = Number(url.searchParams.get('people') || 22);
    const camOff = new Set((url.searchParams.get('off') || '').split(',').filter(Boolean));
    const words = (url.searchParams.get('words') || 'Decrease page,Increase page').split(',');
    const SELF = '99';
    window.__zoom = {
      page: 0,
      people: Array.from({ length: n }, (_, i) => ({ userId: String(1000 + i),
        bVideoOn: !camOff.has(String(1000 + i)), bRaiseHand: false })),
    };
    function ordered() {
      const z = window.__zoom;
      const hands = z.people.filter((p) => p.bRaiseHand);
      const rest = z.people.filter((p) => !p.bRaiseHand)
        .sort((a, b) => Number(b.bVideoOn) - Number(a.bVideoOn));
      rest.splice(hands.length > 0 ? 0 : 1, 0, { userId: SELF, bVideoOn: true, self: true });
      return hands.concat(rest);
    }
    const pageSize = Math.floor(365 / 136.75) * Math.floor(640 / 240);
    function pages() { const t = ordered().length; return t <= pageSize ? 1 : Math.ceil(t / pageSize); }
    function onPage() {
      const all = ordered(); const z = window.__zoom; const P = pages();
      let m = all.filter((_, i) => Math.floor(i / pageSize) === z.page);
      if (m.length < pageSize && P > 1) {
        const s = pageSize - m.length;
        m = all.filter((_, i) => i >= pageSize * (P - 1) - s && i < pageSize * (P - 1)).concat(m);
      }
      return m;
    }
    function render() {
      const tiles = document.getElementById('tiles'); tiles.innerHTML = '';
      for (const p of onPage()) {
        if (p.bVideoOn) {
          const v = document.createElement('video-player');
          v.setAttribute('node-id', p.userId);
          if (p.self) v.setAttribute('media-type', 'preview');
          v.style.display = 'block'; v.style.background = '#0a5'; v.textContent = p.userId;
          tiles.appendChild(v);
        } else {
          const a = document.createElement('div'); a.className = 'avatar';
          a.textContent = p.userId + ' (camera off)'; tiles.appendChild(a);
        }
      }
      let pag = document.querySelector('[aria-label="Pagination"]');
      if (pages() <= 1) { if (pag) pag.remove(); return; }
      if (!pag) {
        pag = document.createElement('div');
        pag.setAttribute('aria-label', 'Pagination');
        pag.style.cssText = 'position:absolute;inset:0;display:flex;justify-content:space-between;align-items:center;pointer-events:none';
        const mk = (label, fn) => {
          const b = document.createElement('button'); b.setAttribute('aria-label', label);
          b.className = 'zoom-MuiButtonBase-root zoom-MuiButton-root zoom-MuiButton-text';
          b.style.pointerEvents = 'auto'; b.textContent = label[0]; b.addEventListener('click', fn);
          return b;
        };
        pag.appendChild(mk(words[0], () => { const z = window.__zoom; if (z.page > 0) { z.page -= 1; render(); } }));
        pag.appendChild(mk(words[1], () => { const z = window.__zoom; if (z.page < pages() - 1) { z.page += 1; render(); } }));
        document.getElementById('gallery').appendChild(pag);
      }
      const [dec, inc] = pag.querySelectorAll('button');
      dec.style.visibility = window.__zoom.page === 0 ? 'hidden' : 'visible';
      inc.style.visibility = window.__zoom.page === pages() - 1 ? 'hidden' : 'visible';
    }
    window.__render = render;
    render();
    window.ready = 1;
  </script>
  <script src="/static/zoom_client.js"></script>
</body></html>"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/static/"):
            f = STATIC / path[len("/static/"):]
            body, ctype = f.read_bytes(), "application/javascript"
        else:
            body, ctype = FIXTURE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve():
    srv = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def _open(browser, port, query):
    page = await browser.new_page(viewport=(800, 600))
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(f"http://127.0.0.1:{port}/?{query}", wait_until="load", timeout=20000)
    await page.wait_for_function("window.ready === 1 && typeof window.zoomGalleryTurn === 'function'",
                                 timeout=10000)
    return page, errors


async def _walk(page, cameras, trips):
    """Turn the way the capture loop does: on until every camera has been
    on screen in this trip, then back to the first page."""
    seen, trip_seen, turns = set(), set(), []
    while trips:
        on = set(await page.evaluate("async () => await window.zoomRenderedVideoUsers()")) - {"99"}
        seen |= on
        trip_seen |= on
        direction = "first" if cameras <= trip_seen else "next"
        turns.append(await page.evaluate("async (d) => await window.zoomGalleryTurn(d)", direction))
        if direction == "first":
            trips -= 1
            trip_seen = set()
        assert len(turns) < 60, "the walk never came round"
    return seen, turns


@needs_chromium
def test_the_bot_turns_zooms_own_gallery_pages():
    async def run():
        srv = _serve()
        browser = await CdpBrowser(CHROMIUM, ["--no-sandbox"]).launch()
        try:
            port = srv.server_address[1]

            # The fault: Zoom's page buttons do not say what the bot used to
            # look for, so it could never press them.
            for label in ("Increase page", "Decrease page"):
                assert not re.search(r"next[ -]?page|prev(ious)?[ -]?page", label, re.I)

            # Session 142's shape: 22 students, 18 cameras on, four tiles a page.
            page, errors = await _open(browser, port, "people=22&off=1018,1019,1020,1021")
            cameras = {str(1000 + i) for i in range(18)}
            seen, turns = await _walk(page, cameras, trips=2)
            assert seen == cameras, f"the bot saw {len(seen)} of 18 cameras"
            assert all(t["ok"] for t in turns), turns
            moved = [t["moved"] for t in turns]
            assert moved == ["next"] * 4 + ["first"] + ["next"] * 4 + ["first"], moved
            assert await page.evaluate("window.__zoom.page") == 0
            info = await page.evaluate("async () => await window.zoomGalleryInfo()")
            assert info["paged"] is True and info["turns"] == 10 and info["failures"] == 0

            # Back to the first page from anywhere, however far in.
            await page.evaluate("() => { window.__zoom.page = 4; window.__render(); }")
            back = await page.evaluate("async () => await window.zoomGalleryTurn('first')")
            assert back["ok"] and back["moved"] == "first" and back["presses"] == 4
            assert await page.evaluate("window.__zoom.page") == 0

            # "next" on the last page comes back round to the first.
            await page.evaluate("() => { window.__zoom.page = 5; window.__render(); }")
            last = await page.evaluate("async () => await window.zoomGalleryTurn('next')")
            assert last["ok"] and last["moved"] == "first"
            assert last["pages"] == 6, "reaching the end is how the number of pages is learned"
            assert errors == []

            # Everybody fits on one page: Zoom draws no buttons, and there is
            # nothing to turn, which is not a fault.
            small, errors = await _open(browser, port, "people=3")
            one = await small.evaluate("async () => await window.zoomGalleryTurn('next')")
            assert one == {"ok": True, "moved": "none", "page": 0, "pages": 1}

            # The words the SDK used before still work.
            older, errors = await _open(browser, port, "people=22&words=Previous page,Next page")
            seen, turns = await _walk(older, cameras | {str(1000 + i) for i in range(18, 22)}, trips=1)
            assert all(t["ok"] for t in turns) and len(seen) == 22

            # Buttons nobody recognises: the answer says so and lists what
            # was there, so the console can show it.
            odd, errors = await _open(browser, port, "people=22&words=Zurueck,Weiter")
            bad = await odd.evaluate("async () => await window.zoomGalleryTurn('next')")
            assert bad["ok"] is False and bad["why"]
            assert any("Weiter" in b for b in bad["buttons"]), bad
            info = await odd.evaluate("async () => await window.zoomGalleryInfo()")
            assert info["failures"] == 1 and info["last"]["ok"] is False
            assert errors == []
        finally:
            await browser.close()
            srv.shutdown()

    asyncio.run(run())
