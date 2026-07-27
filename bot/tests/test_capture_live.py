"""
End-to-end proof of the frame-capture chain, in a real Chromium.

Run:  python -m bot.tests.test_capture_live      (needs Playwright + a Chromium)

Loads the production static/zoom_client.js onto a fixture page whose DOM uses
the exact binding the Zoom SDK uses at runtime (verified in the 3.13.2 bundle):
video tiles carrying `node-id="<userId>"`, `"0"` when detached, and
`media-type="preview"` on the self preview. Then drives the REAL
PlaywrightZoomClient.capture_user() against it and runs the REAL OpenCV face
check on what comes back.

What a live Zoom meeting adds beyond this is only whether the SDK attaches
tiles under our init options; every step after that is exercised here.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import socketserver
import threading
from pathlib import Path

from bot.face import face_present
from bot.meeting_client import PlaywrightZoomClient

STATIC = Path(__file__).resolve().parent.parent / "static"
SCRATCH = Path("/tmp/claude-0/-home-user/2f618078-b502-5e26-b334-4bdcea6bdeaf/scratchpad")
CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

FIXTURE = """<!doctype html>
<html><head><meta charset="utf-8"><title>capture fixture</title>
<style>
  body { margin:0; background:#111; }
  .tile { position:absolute; }
</style></head>
<body>
  <div id="zoom-root"></div>

  <!-- Maria: rendered tile with a real face drawn into a canvas. -->
  <video-player node-id="16778240" class="tile" style="left:0;top:0;width:320px;height:180px">
    <canvas id="c-maria" width="320" height="180"></canvas>
  </video-player>

  <!-- Maria again, as a small thumbnail: the larger tile must win. -->
  <video-player node-id="16778240" class="tile" style="left:0;top:200px;width:64px;height:36px">
    <canvas width="64" height="36"></canvas>
  </video-player>

  <!-- Sam: rendered tile with no face (noise pattern). -->
  <video-player node-id="16778241" class="tile" style="left:340px;top:0;width:320px;height:180px">
    <canvas id="c-sam" width="320" height="180"></canvas>
  </video-player>

  <!-- The bot's own preview: must never be captured as a participant. -->
  <video-player node-id="16778299" media-type="preview" class="tile"
                style="left:680px;top:0;width:320px;height:180px">
    <canvas width="320" height="180"></canvas>
  </video-player>

  <!-- A detached tile: node-id reset to 0, must be ignored. -->
  <video-player node-id="0" class="tile" style="left:0;top:260px;width:320px;height:180px">
    <canvas width="320" height="180"></canvas>
  </video-player>

  <script src="/static/zoom_client.js"></script>
  <script>
    // Paint Maria's tile with the face image, Sam's with noise.
    window.paint = async () => {
      const img = new Image();
      await new Promise((res, rej) => {
        img.onload = res; img.onerror = rej; img.src = '/face.jpg';
      });
      const m = document.getElementById('c-maria').getContext('2d');
      m.drawImage(img, 0, 0, 320, 180);
      const s = document.getElementById('c-sam').getContext('2d');
      const noise = s.createImageData(320, 180);
      for (let i = 0; i < noise.data.length; i += 4) {
        const v = (i * 2654435761) % 255;
        noise.data[i] = v; noise.data[i + 1] = (v * 7) % 255;
        noise.data[i + 2] = (v * 13) % 255; noise.data[i + 3] = 255;
      }
      s.putImageData(noise, 0, 0);
      return true;
    };
  </script>
</body></html>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.split("?")[0] == "/fixture.html":
            body = FIXTURE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *a):
        pass


def _serve(root: Path, port: int):
    handler = functools.partial(Handler, directory=str(root))
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


async def _run() -> None:
    from playwright.async_api import async_playwright

    # Serve the production static/ plus the fixture and the face image.
    root = SCRATCH / "capfix"
    root.mkdir(parents=True, exist_ok=True)
    (root / "static").mkdir(exist_ok=True)
    (root / "static" / "zoom_client.js").write_bytes(
        (STATIC / "zoom_client.js").read_bytes())
    face_src = SCRATCH / "face.jpg"
    assert face_src.exists(), "test face image missing (scratchpad/face.jpg)"
    (root / "face.jpg").write_bytes(face_src.read_bytes())

    port = 8141
    _serve(root, port)

    client = PlaywrightZoomClient(page_url="unused", headless=True)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
    page = await browser.new_page(viewport={"width": 1280, "height": 720})
    client._pw, client._browser, client._page = pw, browser, page

    try:
        await page.goto(f"http://127.0.0.1:{port}/fixture.html", wait_until="load")
        assert await page.evaluate("() => window.paint()") is True

        # 1. A rendered participant yields real PNG bytes of THEIR tile,
        #    and the face check runs on it and finds the face.
        maria = await client.capture_user("16778240")
        assert maria and maria[:8] == b"\x89PNG\r\n\x1a\n", "expected PNG bytes"
        assert face_present(maria) is True, "Haar must find the face in the tile"

        # 2. The other participant's tile is different pixels, no face.
        sam = await client.capture_user("16778241")
        assert sam and sam != maria
        assert face_present(sam) is False, "noise must not read as a face"

        # 3. The larger of two tiles for the same user was chosen.
        mark = await page.evaluate(
            "async () => await window.zoomMarkUserTile('16778240')")
        assert mark["ok"] and mark["width"] == 320, mark
        await page.evaluate("async () => await window.zoomUnmarkTile()")

        # 4. The self preview is never treated as a participant tile.
        assert await client.capture_user("16778299") is None

        # 5. Unknown users and detached (node-id=0) tiles yield nothing,
        #    and the miss reports what IS rendered for diagnosability.
        assert await client.capture_user("55555") is None
        miss = await page.evaluate(
            "async () => await window.zoomMarkUserTile('55555')")
        assert miss["ok"] is False
        assert sorted(miss["rendered"]) == ["16778240", "16778241"], miss

        # 6. No mark left behind after capture.
        leftovers = await page.evaluate(
            "() => document.querySelectorAll('[data-cap-target]').length")
        assert leftovers == 0

        # 7. The diagnostics roster of rendered users matches.
        rendered = await page.evaluate(
            "async () => await window.zoomRenderedVideoUsers()")
        assert sorted(rendered) == ["16778240", "16778241"]
    finally:
        await client.close()

    print("CAPTURE CHAIN OK (marking, screenshot, exclusions, face detection)")


def run() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    run()
