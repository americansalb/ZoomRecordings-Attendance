"""
Chromium over its own DevTools pipe: the browser driver with no helper.

Playwright drives Chromium through a Node.js program that sits between
Python and the browser and relays every command. Measured live on the
512 MB bot machine, that relay is about 60 MB of a budget the browser
itself needs. This module talks to Chromium directly over the same
channel Playwright uses underneath (the DevTools protocol on a pipe the
browser opens as file descriptors 3 and 4), so the relay program never
starts.

It deliberately speaks the tiny surface meeting_client.py uses, in the
same shape: a browser with new_page() and close(), a page with
evaluate(), goto(), wait_for_function(), expose_function(), screenshot(),
locator(selector).screenshot(), and on('console' | 'pageerror' |
'requestfailed' | 'crash'). Nothing else, on purpose: the meeting client
does not care which driver it holds, and the Playwright driver stays
available behind BOT_BROWSER_DRIVER=playwright as the way back.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BrowserGone(RuntimeError):
    """The browser process is not there any more. The words are chosen so
    the death classifiers here and on the control plane recognise them."""


class PageError(RuntimeError):
    """A script the page ran threw. Zoom's own refusal reads like this."""


class CdpError(RuntimeError):
    """The browser answered a command with an error."""


class CdpTimeout(TimeoutError):
    pass


# Chromium flags Playwright itself passes, kept for the ones that matter
# in a container: a page that never goes to sleep because it is "in the
# background" (every headless page is), no first-run and update chatter,
# no crash reporter, no extensions, no throttled timers. The Zoom SDK
# keeps a meeting alive with timers, and a throttled timer drops the
# meeting.
CHROMIUM_FLAGS = [
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--disable-hang-monitor",
    "--disable-breakpad",
    "--disable-component-update",
    "--disable-component-extensions-with-background-pages",
    "--disable-extensions",
    "--disable-default-apps",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--disable-client-side-phishing-detection",
    "--disable-field-trial-config",
    "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,"
    "DialMediaRouteProvider,GlobalMediaControls,OptimizationHints,"
    "DestroyProfileOnBrowserClose,PaintHolding,LensOverlay",
    "--allow-pre-commit-input",
    "--force-color-profile=srgb",
    "--metrics-recording-only",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--password-store=basic",
    "--use-mock-keychain",
    "--enable-automation",
    "--hide-scrollbars",
    "--mute-audio",
]


def _revision(path: str) -> int:
    m = re.search(r"chromium(?:_headless_shell)?-(\d+)/", path)
    return int(m.group(1)) if m else 0


def find_chromium(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Where Chromium is on this machine.

    BOT_CHROMIUM_PATH wins. Otherwise the browsers Playwright installed
    (the Docker image installs them at build time, under
    PLAYWRIGHT_BROWSERS_PATH or /ms-playwright): the headless shell first,
    which is the lean build Playwright itself runs headless sessions on,
    then the full browser, newest revision first. A system Chromium last.
    """
    env = os.environ if env is None else env
    explicit = (env.get("BOT_CHROMIUM_PATH") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    roots = [env.get("PLAYWRIGHT_BROWSERS_PATH"), "/ms-playwright",
             os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for pattern in ("chromium_headless_shell-*/chrome-linux/headless_shell",
                        "chromium-*/chrome-linux/chrome"):
            hits = [h for h in glob.glob(os.path.join(root, pattern)) if os.access(h, os.X_OK)]
            if hits:
                hits.sort(key=_revision, reverse=True)
                return hits[0]
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def headless_flag(executable: str) -> str:
    """The headless shell is headless by construction and takes the plain
    flag; the full browser needs the new headless mode named."""
    return "--headless" if os.path.basename(executable) == "headless_shell" else "--headless=new"


def split_frames(buffer: bytes) -> Tuple[List[bytes], bytes]:
    """DevTools pipe framing: one JSON message, then a NUL byte. Returns
    the complete messages and whatever is left unfinished."""
    parts = buffer.split(b"\0")
    return parts[:-1], parts[-1]


def evaluate_source_is_function(source: str) -> bool:
    """Playwright's rule, in short: a string that is a function is called
    with the argument; anything else is an expression."""
    s = source.strip()
    if s.startswith(("async ", "function", "async(")):
        return True
    return bool(re.match(r"^\(?[\w\s,\[\]{}:=]*\)?\s*=>", s))


class ConsoleMessage:
    def __init__(self, type_: str, text: str):
        self.type = type_
        self.text = text


class FailedRequest:
    def __init__(self, url: str, failure: str):
        self.url = url
        self.failure = failure


class CdpBrowser:
    """One Chromium process and the pipe to it."""

    def __init__(self, executable: str, args: List[str], headless: bool = True,
                 wiring: Optional[str] = None):
        self.executable = executable
        self.args = list(args)
        self.headless = headless
        # How fds 3 and 4 are wired: "bash" (a bash that moves the pipe
        # ends and then becomes Chromium) or "hook" (a pre-exec hook, with
        # Python told to leave descriptors alone). Auto picks bash when
        # there is one; dash cannot address descriptors above 9.
        self.wiring = wiring or ("bash" if shutil.which("bash") else "hook")
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer = None
        self._profile_dir: Optional[str] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._listeners: Dict[Tuple[Optional[str], str], List[Callable[[dict], None]]] = {}
        self._disconnect_handlers: List[Callable[[], None]] = []
        self._tasks: List[asyncio.Task] = []
        self._stderr_tail: List[str] = []
        self.closed = False
        self._gone_reason: Optional[str] = None

    # ── lifecycle ────────────────────────────────────────────────────

    async def launch(self) -> "CdpBrowser":
        self._profile_dir = tempfile.mkdtemp(prefix="bot-chromium-")
        cmd = [self.executable, *CHROMIUM_FLAGS]
        if self.headless:
            cmd.append(headless_flag(self.executable))
        cmd += [f"--user-data-dir={self._profile_dir}", "--remote-debugging-pipe",
                *self.args, "about:blank"]

        # The browser reads commands on fd 3 and writes answers on fd 4.
        child_read, parent_write = os.pipe()
        parent_read, child_write = os.pipe()
        common = dict(stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                      stderr=asyncio.subprocess.PIPE)
        try:
            if self.wiring == "bash":
                # The pipe ends go to bash at whatever numbers they have;
                # bash moves them to 3 and 4 and then becomes Chromium.
                script = f'exec 3<&{child_read} 4>&{child_write}; exec "$0" "$@"'
                self._proc = await asyncio.create_subprocess_exec(
                    shutil.which("bash") or "/bin/bash", "-c", script, *cmd,
                    pass_fds=(child_read, child_write), **common)
            else:
                # A pre-exec hook moves them. Python must then be told not
                # to close descriptors (it would close 3 and 4 right after
                # the hook); everything else of this process is marked
                # close-on-exec by default, so only the pipe ends survive.
                os.set_inheritable(child_read, True)
                os.set_inheritable(child_write, True)

                def _wire():
                    os.dup2(child_read, 3)
                    os.dup2(child_write, 4)

                self._proc = await asyncio.create_subprocess_exec(
                    *cmd, close_fds=False, preexec_fn=_wire, **common)
        finally:
            os.close(child_read)
            os.close(child_write)

        loop = asyncio.get_running_loop()
        self._reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
        protocol = asyncio.StreamReaderProtocol(self._reader)
        await loop.connect_read_pipe(lambda: protocol, os.fdopen(parent_read, "rb", 0))
        write_transport, _ = await loop.connect_write_pipe(
            asyncio.Protocol, os.fdopen(parent_write, "wb", 0))
        self._writer = write_transport

        self._tasks.append(asyncio.create_task(self._read_loop()))
        self._tasks.append(asyncio.create_task(self._stderr_loop()))
        self._tasks.append(asyncio.create_task(self._exit_watch()))
        # Prove the channel works before handing the browser out.
        await self.send("Browser.getVersion", timeout=30)
        return self

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        proc = self._proc
        try:
            if proc and proc.returncode is None:
                try:
                    await self.send("Browser.close", timeout=5)
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=8)
                except (asyncio.TimeoutError, Exception):
                    for stop in (proc.terminate, proc.kill):
                        try:
                            stop()
                            await asyncio.wait_for(proc.wait(), timeout=5)
                            break
                        except Exception:
                            continue
        finally:
            self._teardown("closed")

    def _teardown(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(BrowserGone(f"browser has been closed ({reason})"))
        self._pending.clear()
        for t in self._tasks:
            if t is not asyncio.current_task() and not t.done():
                t.cancel()
        self._tasks = []
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
            self._profile_dir = None

    def on_disconnected(self, handler: Callable[[], None]) -> None:
        self._disconnect_handlers.append(handler)

    # Playwright shape: browser.on("disconnected", fn).
    def on(self, event: str, handler: Callable[..., None]) -> None:
        if event == "disconnected":
            self.on_disconnected(handler)

    @property
    def stderr_tail(self) -> List[str]:
        return list(self._stderr_tail)

    async def _exit_watch(self) -> None:
        proc = self._proc
        if not proc:
            return
        code = await proc.wait()
        if self.closed:
            return
        self._gone_reason = f"the browser process died (exit code {code})"
        logger.warning("[BOT] %s; last stderr: %s", self._gone_reason,
                       " | ".join(self._stderr_tail[-3:]))
        self.closed = True
        self._teardown(self._gone_reason)
        for h in list(self._disconnect_handlers):
            try:
                h()
            except Exception as e:
                logger.warning("disconnect handler error: %s", e)

    async def _stderr_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr_tail.append(text[:300])
                    del self._stderr_tail[:-30]
        except (asyncio.CancelledError, Exception):
            return

    async def _read_loop(self) -> None:
        reader = self._reader
        buffer = b""
        try:
            while True:
                chunk = await reader.read(256 * 1024)
                if not chunk:
                    return
                frames, buffer = split_frames(buffer + chunk)
                for frame in frames:
                    if frame:
                        self._dispatch(frame)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("[BOT] devtools pipe read failed: %s", e)

    def _dispatch(self, frame: bytes) -> None:
        try:
            msg = json.loads(frame)
        except ValueError:
            return
        if "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        method = msg.get("method")
        if not method:
            return
        params = msg.get("params") or {}
        session = msg.get("sessionId")
        for key in ((session, method), (None, method)):
            for handler in list(self._listeners.get(key, [])):
                try:
                    handler(params)
                except Exception as e:
                    logger.warning("devtools event handler %s failed: %s", method, e)

    # ── commands and events ──────────────────────────────────────────

    async def send(self, method: str, params: Optional[dict] = None,
                   session_id: Optional[str] = None, timeout: Optional[float] = 30) -> dict:
        if self.closed or self._writer is None:
            raise BrowserGone(f"browser has been closed ({self._gone_reason or 'closed'})")
        self._next_id += 1
        msg_id = self._next_id
        msg: Dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        self._writer.write(json.dumps(msg).encode("utf-8") + b"\0")
        try:
            reply = await (asyncio.wait_for(fut, timeout) if timeout else fut)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise CdpTimeout(f"{method} got no answer from the browser in {timeout}s") from None
        if "error" in reply:
            err = reply["error"] or {}
            raise CdpError(f"{method}: {err.get('message') or err}")
        return reply.get("result") or {}

    def listen(self, method: str, handler: Callable[[dict], None],
               session_id: Optional[str] = None) -> None:
        self._listeners.setdefault((session_id, method), []).append(handler)

    async def new_page(self, viewport: Tuple[int, int] = (800, 600)) -> "CdpPage":
        """One tab in the browser's default context. Not a private
        (incognito) context: every launch already has its own throwaway
        profile, so there is nothing to keep apart, and measured here a
        page created inside a private context kills a --single-process
        Chromium outright (the lookout diet runs single-process)."""
        target = await self.send("Target.createTarget", {
            "url": "about:blank", "width": viewport[0], "height": viewport[1]})
        attached = await self.send("Target.attachToTarget",
                                   {"targetId": target["targetId"], "flatten": True})
        page = CdpPage(self, attached["sessionId"], target["targetId"], None)
        await page._init(viewport)
        return page


class _Locator:
    def __init__(self, page: "CdpPage", selector: str):
        self._page = page
        self._selector = selector

    async def screenshot(self, type: str = "png", timeout: Optional[float] = None, **_ignored) -> bytes:
        return await self._page.element_screenshot(self._selector, timeout=timeout)


class CdpPage:
    """One tab, with the surface meeting_client.py uses."""

    BRIDGE = "__botBridge"

    def __init__(self, browser: CdpBrowser, session_id: str, target_id: str,
                 context_id: Optional[str] = None):
        self.browser = browser
        self.session_id = session_id
        self.target_id = target_id
        self.context_id = context_id
        self._exposed: Dict[str, Callable[[Any], Awaitable[None]]] = {}
        self._handlers: Dict[str, List[Callable[..., None]]] = {}
        self._requests: Dict[str, str] = {}
        self._dom_loaded: List[asyncio.Future] = []
        self._loaded: List[asyncio.Future] = []

    async def _send(self, method: str, params: Optional[dict] = None,
                    timeout: Optional[float] = 30) -> dict:
        return await self.browser.send(method, params, session_id=self.session_id, timeout=timeout)

    async def _init(self, viewport: Tuple[int, int]) -> None:
        b = self.browser
        b.listen("Runtime.bindingCalled", self._on_binding, self.session_id)
        b.listen("Runtime.exceptionThrown", self._on_exception, self.session_id)
        b.listen("Runtime.consoleAPICalled", self._on_console, self.session_id)
        b.listen("Network.requestWillBeSent", self._on_request, self.session_id)
        b.listen("Network.loadingFailed", self._on_request_failed, self.session_id)
        b.listen("Inspector.targetCrashed", lambda _p: self._emit("crash", self), self.session_id)
        b.listen("Page.domContentEventFired", lambda _p: self._settle(self._dom_loaded), self.session_id)
        b.listen("Page.loadEventFired", lambda _p: self._settle(self._loaded), self.session_id)
        await self._send("Page.enable")
        await self._send("Runtime.enable")
        await self._send("Network.enable")
        await self._send("Emulation.setDeviceMetricsOverride", {
            "width": viewport[0], "height": viewport[1], "deviceScaleFactor": 1, "mobile": False})
        await self._send("Runtime.addBinding", {"name": self.BRIDGE})

    # ── events in Playwright's shape ─────────────────────────────────

    def on(self, event: str, handler: Callable[..., None]) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def _emit(self, event: str, *args) -> None:
        for h in list(self._handlers.get(event, [])):
            try:
                h(*args)
            except Exception as e:
                logger.warning("page %s handler failed: %s", event, e)

    @staticmethod
    def _settle(waiters: List[asyncio.Future]) -> None:
        for fut in waiters:
            if not fut.done():
                fut.set_result(True)
        waiters.clear()

    def _on_binding(self, params: dict) -> None:
        if params.get("name") != self.BRIDGE:
            return
        try:
            call = json.loads(params.get("payload") or "{}")
        except ValueError:
            return
        fn = self._exposed.get(str(call.get("name")))
        if fn is None:
            return
        try:
            asyncio.get_running_loop().create_task(fn(call.get("payload")))
        except RuntimeError:
            pass

    def _on_exception(self, params: dict) -> None:
        d = params.get("exceptionDetails") or {}
        exc = d.get("exception") or {}
        text = exc.get("description") or d.get("text") or "page error"
        self._emit("pageerror", RuntimeError(text))

    def _on_console(self, params: dict) -> None:
        parts = []
        for a in params.get("args") or []:
            if "value" in a:
                parts.append(str(a["value"]))
            else:
                parts.append(str(a.get("description") or a.get("type") or ""))
        self._emit("console", ConsoleMessage(str(params.get("type") or "log"), " ".join(parts)))

    def _on_request(self, params: dict) -> None:
        req = params.get("request") or {}
        rid = params.get("requestId")
        if rid and req.get("url"):
            self._requests[rid] = req["url"]
            if len(self._requests) > 400:
                for key in list(self._requests)[:100]:
                    self._requests.pop(key, None)

    def _on_request_failed(self, params: dict) -> None:
        url = self._requests.pop(params.get("requestId"), "") or "(unknown url)"
        self._emit("requestfailed", FailedRequest(url, str(params.get("errorText") or "failed")))

    # ── the surface ──────────────────────────────────────────────────

    async def expose_function(self, name: str, fn: Callable[[Any], Awaitable[None]]) -> None:
        """window.<name>(payload) in the page reaches fn(payload) here.
        Installed for every future document and the current one."""
        self._exposed[name] = fn
        shim = (f"window[{json.dumps(name)}] = (payload) => "
                f"window.{self.BRIDGE}(JSON.stringify({{name: {json.dumps(name)}, payload: payload === undefined ? null : payload}}));")
        await self._send("Page.addScriptToEvaluateOnNewDocument", {"source": shim})
        await self._send("Runtime.evaluate", {"expression": shim, "returnByValue": True})

    async def goto(self, url: str, wait_until: str = "load", timeout: Optional[float] = 30_000) -> None:
        waiters = self._dom_loaded if wait_until == "domcontentloaded" else self._loaded
        fut = asyncio.get_running_loop().create_future()
        waiters.append(fut)
        result = await self._send("Page.navigate", {"url": url})
        if result.get("errorText"):
            raise RuntimeError(f"navigation to {url} failed: {result['errorText']}")
        try:
            await asyncio.wait_for(fut, (timeout or 30_000) / 1000.0)
        except asyncio.TimeoutError:
            raise CdpTimeout(f"{url} did not reach {wait_until} in {timeout}ms") from None

    async def evaluate(self, source: str, arg: Any = None, timeout: Optional[float] = 600) -> Any:
        """Playwright's page.evaluate: a function source is called with the
        argument, an expression is evaluated; promises are awaited; the
        result comes back as JSON. A throw comes back as PageError with
        the page's own words."""
        if evaluate_source_is_function(source):
            probe = await self._send("Runtime.evaluate", {
                "expression": f"({source})", "returnByValue": False}, timeout=timeout)
            self._raise_if_thrown(probe)
            obj = probe.get("result") or {}
            if obj.get("type") != "function" or not obj.get("objectId"):
                raise PageError("evaluate: the source is not a function")
            try:
                reply = await self._send("Runtime.callFunctionOn", {
                    "objectId": obj["objectId"],
                    "functionDeclaration": "function (a) { return this(a); }",
                    "arguments": [{"value": arg}],
                    "awaitPromise": True, "returnByValue": True, "userGesture": True,
                }, timeout=timeout)
            finally:
                try:
                    await self._send("Runtime.releaseObject", {"objectId": obj["objectId"]}, timeout=5)
                except Exception:
                    pass
        else:
            reply = await self._send("Runtime.evaluate", {
                "expression": source, "awaitPromise": True, "returnByValue": True,
                "userGesture": True}, timeout=timeout)
        self._raise_if_thrown(reply)
        return (reply.get("result") or {}).get("value")

    @staticmethod
    def _raise_if_thrown(reply: dict) -> None:
        d = reply.get("exceptionDetails")
        if not d:
            return
        exc = d.get("exception") or {}
        text = exc.get("description") or exc.get("value") or d.get("text") or "page script threw"
        raise PageError(str(text)[:600])

    async def wait_for_function(self, expression: str, timeout: Optional[float] = 30_000,
                                interval: float = 0.25) -> None:
        deadline = time.monotonic() + (timeout or 30_000) / 1000.0
        while True:
            try:
                if await self.evaluate(expression, timeout=30):
                    return
            except PageError:
                pass
            if time.monotonic() >= deadline:
                raise CdpTimeout(f"waiting for {expression[:80]} timed out after {timeout}ms")
            await asyncio.sleep(interval)

    async def screenshot(self, type: str = "png", timeout: Optional[float] = None, **_ignored) -> bytes:
        seconds = (timeout / 1000.0) if timeout else 30
        reply = await self._send("Page.captureScreenshot", {"format": type}, timeout=seconds)
        return base64.b64decode(reply.get("data") or "")

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def element_screenshot(self, selector: str, timeout: Optional[float] = None) -> bytes:
        seconds = (timeout / 1000.0) if timeout else 30
        box = await self.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                const r = el.getBoundingClientRect();
                return { x: r.x, y: r.y, width: r.width, height: r.height };
            }""", selector, timeout=seconds)
        if not box or not box.get("width") or not box.get("height"):
            raise RuntimeError(f"no visible element for {selector}")
        reply = await self._send("Page.captureScreenshot", {
            "format": "png",
            "clip": {"x": box["x"], "y": box["y"], "width": box["width"],
                     "height": box["height"], "scale": 1},
        }, timeout=seconds)
        return base64.b64decode(reply.get("data") or "")

    async def close(self) -> None:
        try:
            await self.browser.send("Target.closeTarget", {"targetId": self.target_id}, timeout=5)
        except Exception:
            pass
