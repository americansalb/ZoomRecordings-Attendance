"""
Meeting client abstraction.

The capture loop and bot manager talk to a meeting through this interface so the
orchestration is testable without a real meeting:

  - MeetingClient: the interface.
  - FakeMeetingClient: in-memory, for tests.
  - ChromiumZoomClient: drives a headless Chromium running the Zoom Web SDK
    (see static/zoom_client.html + zoom_client.js), through one of two
    drivers: CdpZoomClient talks to the browser directly over its
    DevTools pipe (bot/cdp.py, no helper program, the default), and
    PlaywrightZoomClient goes through Playwright's Node.js relay, kept
    behind BOT_BROWSER_DRIVER=playwright as the way back.

Identity note: attendance is attributed by Zoom user id, never by tile position.
`presence()` returns the per-user ledger the browser page maintains from Zoom's
own user-added/user-removed/user-updated events.

Capture note: per-user frames come from screenshotting the tile the SDK itself
bound to the user (its node-id attribute), via Playwright's compositor capture.
Only tiles Zoom has attached are capturable, so capture_user returns None for
anyone off the rendered gallery page and the caller records that no check ran.
video_on from Zoom's per-user state remains authoritative for attendance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional


def image_bytes_to_y4m(data: bytes, path: str, width: int = 640, height: int = 360,
                       fps: int = 10) -> bool:
    """Decode an image (JPEG, PNG, WebP) and write it as a two-frame Y4M
    file Chromium's fake webcam can play, letterboxed onto 16:9 so the
    whole picture shows in a Zoom tile. False when the image cannot be
    decoded; the caller keeps the built-in picture then.

    The work happens in a child process (y4m_convert.py) so this process
    never imports OpenCV and NumPy: measured live, they added about 38 MB
    to the bot for the whole session, paid even when the picture was then
    declined by the memory gate.
    """
    import subprocess
    import sys
    import tempfile
    script = str(Path(__file__).parent / "y4m_convert.py")
    fd, in_path = tempfile.mkstemp(prefix="bot-face-in-")
    os.close(fd)
    try:
        with open(in_path, "wb") as f:
            f.write(data)
        r = subprocess.run(
            [sys.executable, script, in_path, path, str(width), str(height), str(fps)],
            capture_output=True, timeout=60)
        return r.returncode == 0 and os.path.exists(path) and os.path.getsize(path) > 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(in_path)
        except OSError:
            pass


_CAMERA_WORDS = re.compile(
    r"video|camera|encod|getUserMedia|NotAllowed|NotReadable|permission|mediaDevices", re.I)


def _looks_like_browser_death(exc: BaseException) -> bool:
    """Distinguish 'the browser died under us' from 'Zoom said no'.

    The join retries on normal flags only for the first kind: a rejected
    passcode or a waiting room denial would fail identically on any flag
    set, and retrying those doubles the worst-case join time for nothing.
    """
    text = f"{type(exc).__name__}: {exc}"
    return bool(re.search(
        r"Target (page|context|browser).*?(closed|crashed)"
        r"|browser has been closed"
        r"|[Pp]age crashed"
        r"|[Bb]rowser.*disconnected"
        r"|Connection closed while reading from the driver"
        r"|BrowserGone|browser process died|has been closed"
        r"|no answer from the browser",
        text))

logger = logging.getLogger(__name__)


def _camera_face_max_people() -> int:
    """Rooms bigger than this never get the camera picture. From
    BOT_CAMERA_FACE_MAX_PEOPLE, default 10, 0 means no limit."""
    try:
        return max(0, int(os.environ.get("BOT_CAMERA_FACE_MAX_PEOPLE", "10") or 10))
    except ValueError:
        return 10


def _gallery_tiles() -> int:
    """Tiles per gallery page, from BOT_GALLERY_TILES, clamped 4 to 25.

    Default 9, not Zoom's 25 ceiling. Every rendered tile is a live video
    decoder, and decoders are what actually fill a 512 MB container: a
    25-tile landing spiked memory hard enough to kill a container before
    its first observation, and left no headroom for the face detector.
    Camera on/off for the WHOLE room comes from the roster regardless of
    tiles; only face checks ride the rendered page, and the gallery
    rotation still covers everyone a page at a time.
    """
    try:
        return max(4, min(25, int(os.environ.get("BOT_GALLERY_TILES", "9") or 9)))
    except ValueError:
        return 9

ChatHandler = Callable[[dict], Awaitable[None]]
LifecycleHandler = Callable[[str, Optional[str]], Awaitable[None]]


@dataclass
class Participant:
    user_id: str
    name: str
    video_on: bool = False
    is_host: bool = False
    is_co_host: bool = False
    # Zoom's waiting room flag. On hold means outside the meeting: not
    # present, not observable, never messageable.
    is_hold: bool = False


@dataclass
class PresenceRow:
    """One participant's attendance record for this meeting."""
    user_id: str
    name: str
    joined_at: float
    left_at: Optional[float]
    present: bool
    video_on: bool
    video_on_seconds: int
    observed_seconds: int


@dataclass
class PresenceSnapshot:
    at: float
    self_user_id: Optional[str]
    joined: bool
    rows: List[PresenceRow] = field(default_factory=list)


class MeetingClient(ABC):
    async def watcher_state(self) -> Optional[dict]:
        """Live seat watcher report, when the client has one. None means
        no watcher, and the sweep falls back to screenshot capture."""
        return None
    on_chat: Optional[ChatHandler] = None
    on_lifecycle: Optional[LifecycleHandler] = None

    @abstractmethod
    async def join(self, *, meeting_number: str, passcode: str, display_name: str,
                   signature: str, sdk_key: str, zak: Optional[str] = None,
                   lookout: bool = False) -> None: ...

    @abstractmethod
    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> bool:
        """Send a chat message. Returns True only when Zoom's own echo of the
        message came back, which is the only proof it was distributed. False
        means the SDK accepted the send and Zoom never carried it."""
        ...

    @abstractmethod
    async def list_participants(self) -> List[Participant]: ...

    @abstractmethod
    async def presence(self) -> PresenceSnapshot:
        """Per-user attendance ledger: who is here, since when, camera time."""

    @abstractmethod
    async def capture_user(self, user_id: str) -> Optional[bytes]:
        """PNG bytes of the user's video, or None when capture is unavailable."""

    async def capture_supported(self) -> bool:
        return False

    async def diagnostics(self) -> dict:
        """Raw SDK state, for diagnosing disputed camera state."""
        return {}

    async def page_screenshot(self) -> Optional[bytes]:
        """PNG of the whole meeting page, for the console's evidence view."""
        return None

    async def stop_video(self) -> Optional[dict]:
        """Switch the bot's own camera picture off, if it has one."""
        return None

    async def gallery_info(self) -> dict:
        """How many gallery pages the room spans right now, for the grid
        proctor's coverage maths. {} when the client cannot say."""
        return {}

    @abstractmethod
    async def leave(self) -> None: ...

    async def self_user_id(self) -> Optional[str]:
        return None


class FakeMeetingClient(MeetingClient):
    """In-memory meeting for tests."""

    def __init__(self, participants: Optional[List[Participant]] = None,
                 frames: Optional[dict] = None,
                 presence_rows: Optional[List[PresenceRow]] = None,
                 self_id: Optional[str] = None):
        self._participants = participants or []
        self._frames = frames or {}   # user_id -> bytes
        self._presence_rows = presence_rows
        self._self_id = self_id
        self.sent_chats: list[dict] = []
        self.joined = False

    async def join(self, **kwargs) -> None:
        self.joined = True

    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> bool:
        self.sent_chats.append({"text": text, "to": to_user_id})
        return True

    async def list_participants(self) -> List[Participant]:
        return list(self._participants)

    async def presence(self) -> PresenceSnapshot:
        if self._presence_rows is not None:
            rows = list(self._presence_rows)
        else:
            # Derive a plausible ledger from the participant list so tests that
            # only care about attribution don't have to build one by hand.
            rows = [
                PresenceRow(user_id=p.user_id, name=p.name, joined_at=0.0, left_at=None,
                            present=True, video_on=p.video_on,
                            video_on_seconds=60 if p.video_on else 0,
                            observed_seconds=60)
                for p in self._participants
            ]
        return PresenceSnapshot(at=0.0, self_user_id=self._self_id, joined=self.joined, rows=rows)

    async def capture_user(self, user_id: str) -> Optional[bytes]:
        return self._frames.get(user_id)

    async def capture_supported(self) -> bool:
        return bool(self._frames)

    async def leave(self) -> None:
        self.joined = False

    async def self_user_id(self) -> Optional[str]:
        return self._self_id

    async def gallery_info(self) -> dict:
        pages = max(1, int(getattr(self, "gallery_pages", 1)))
        return {"pages": pages, "participants": len(self._participants),
                "tilesPerPage": 25}

    async def gallery_advance(self) -> dict:
        self.gallery_advances = getattr(self, "gallery_advances", 0) + 1
        return {"ok": True, "moved": "next"}

    async def page_screenshot(self) -> Optional[bytes]:
        self.page_screenshots = getattr(self, "page_screenshots", 0) + 1
        return b"\x89PNG\r\n\x1a\n fake room grid"

    async def diagnostics(self) -> dict:
        return {"joined": self.joined, "selfUserId": self._self_id,
                "raw": [{"userId": p.user_id, "displayName": p.name,
                         "resolvedVideoOn": p.video_on} for p in self._participants]}

    async def inject_chat(self, event: dict) -> None:
        if self.on_chat:
            await self.on_chat(event)

    async def inject_lifecycle(self, event_type: str, detail: Optional[str] = None) -> None:
        if self.on_lifecycle:
            await self.on_lifecycle(event_type, detail)


class ChromiumZoomClient(MeetingClient):
    """Drives the Zoom Web SDK inside a headless Chromium.

    Everything about the meeting lives here; the three _driver_* hooks
    are the only place a subclass says how the browser is started,
    handed out and closed. Both drivers give back a page with the same
    small surface (evaluate, goto, wait_for_function, expose_function,
    screenshot, locator().screenshot, on), so nothing below the hooks
    knows which one is running.
    """

    DRIVER = "abstract"

    async def _driver_start(self) -> None:
        """Whatever must exist before a browser can be launched."""

    async def _driver_launch(self, args: list):
        """Launch Chromium with these flags; return (browser, context, page)."""
        raise NotImplementedError

    async def _driver_close(self) -> None:
        """Close what _driver_launch opened; safe on a half-built client."""

    def __init__(self, page_url: str, headless: bool = True):
        self.page_url = page_url
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._page_errors: List[str] = []
        self._page_console: List[str] = []
        # Why each face capture worked or did not. Without this, "no frames
        # checked" could mean a camera-off student, an unrendered tile, or
        # a screenshot that timed out, and the console had to guess. It
        # guessed wrong for a whole class.
        self._capture_log: List[dict] = []
        # Teardown vs death bookkeeping. _closing marks an intentional
        # close so the death handlers stay quiet; _joining marks the join
        # window, where the join's own retry logic owns crash recovery;
        # _gone_reported makes the death report once; _diet_active says
        # the collapsed single-process browser is the one running.
        self._closing = False
        self._joining = False
        self._gone_reported = False
        self._diet_active = False
        # A picture from the console's pool for this join, converted to
        # the fake webcam's format in a temp file; None means built-in.
        self._camera_face_path: Optional[str] = None

    # Flags: fake media so Chromium grants mic/cam without hardware, and the
    # WebRTC bits the Web SDK needs in a container.
    BASE_ARGS = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        "--no-sandbox",
        # Containers give /dev/shm 64MB by default and Chromium keeps
        # decoded video frames there. A meeting with several cameras
        # exhausts it and the renderer dies mid-meeting with no error
        # that reaches Python.
        "--disable-dev-shm-usage",
    ]

    # The lookout diet. Chromium normally runs as several separate
    # programs (browser, renderer, GPU, network), each with its own heap;
    # collapsing to one process measured about 20 percent off the whole
    # engine's memory (PSS, fair accounting) with the Zoom SDK loaded. A
    # lookout renders four thumbnails and takes no screenshots, so it has
    # nothing to lose from the rougher mode: a crash of the one process
    # equals a crash of the whole browser, which is already how every
    # failure here ends. Not used for video watching sessions: those do
    # real rendering and capture, and get the well-trodden path.
    LOOKOUT_ARGS = [
        "--single-process",
        "--no-zygote",
        "--disable-gpu",
        "--mute-audio",
    ]

    # The bot's camera picture: a still image Chromium's fake webcam plays
    # on a loop (bot/assets/bot-face.y4m, 640x360, 10 frames a second).
    # Zoom shows a profile photo only for a signed-in account and this bot
    # is a guest, so the picture rides the camera instead. The flag only
    # decides what the fake webcam shows; the cost arrives when the page
    # switches video on, which it does only when memory has room, and the
    # capture loop switches it off again at the hard limit.
    # BOT_CAMERA_FACE=off removes it without a build.
    CAMERA_FACE_FILE = Path(__file__).parent / "assets" / "bot-face.y4m"
    # The camera picture turns the bot's OWN video on, which runs a video
    # encoder for the whole session. On the 512 MB machine that is memory
    # the box does not always have to spare: a real class wedged the
    # container at its ceiling with the cat running (2026-09-05). Measured
    # against the working-set meter, an idle two-person room sits near 50
    # percent, so a limit of 60 lets the cat show in a quiet room while
    # keeping it off once a real class has filled the box. The valves in
    # capture.py still shed it if memory climbs after the join.
    CAMERA_FACE_MAX_MEM = 0.60

    @classmethod
    def camera_face_switched_on(cls) -> bool:
        return os.environ.get("BOT_CAMERA_FACE", "on").strip().lower() not in ("0", "off", "false", "no")

    @classmethod
    def camera_face_enabled(cls) -> bool:
        return cls.camera_face_switched_on() and cls.CAMERA_FACE_FILE.is_file()

    def _face_file(self) -> Path:
        """The picture for this join: one from the pool if the control
        plane sent one, otherwise the built-in one."""
        if self._camera_face_path:
            return Path(self._camera_face_path)
        return self.CAMERA_FACE_FILE

    def set_camera_face(self, image_bytes: Optional[bytes]) -> bool:
        """Wear this picture for the coming join instead of the built-in
        one. Converted into the fake webcam's format in a temp file that
        close() removes. False leaves the built-in picture in place."""
        if not image_bytes:
            return False
        import tempfile
        fd, path = tempfile.mkstemp(prefix="bot-face-", suffix=".y4m")
        os.close(fd)
        if image_bytes_to_y4m(image_bytes, path):
            self._camera_face_path = path
            return True
        try:
            os.unlink(path)
        except OSError:
            pass
        return False

    def _launch_args(self, lookout: bool) -> list:
        args = list(self.BASE_ARGS)
        face = self._face_file()
        if self.camera_face_switched_on() and face.is_file():
            args.append(f"--use-file-for-fake-video-capture={face}")
        if lookout:
            args += self.LOOKOUT_ARGS
        return args

    def _camera_face_now(self) -> bool:
        """Whether this join should switch the camera picture on at all.

        Measured right before the join, with the browser and SDK already
        loaded: a machine already near the line gets no cosmetics.
        """
        return self._camera_face_decision()[0]

    def _camera_face_decision(self):
        """(wanted, reason): the reason is what the console shows when the
        picture stays off, in words the owner can act on."""
        if not self.camera_face_switched_on():
            return False, "switched off with BOT_CAMERA_FACE"
        if not self._face_file().is_file():
            return False, "no picture file on the machine"
        from .capture import CaptureLoop
        frac = CaptureLoop.memory_fraction()
        if frac >= self.CAMERA_FACE_MAX_MEM:
            logger.info("[BOT] camera picture skipped: memory at %d%% before the join",
                        int(frac * 100))
            return False, f"memory was at {int(frac * 100)} percent before the join, the limit is {int(self.CAMERA_FACE_MAX_MEM * 100)}"
        return True, f"memory at {int(frac * 100)} percent before the join"

    async def _open_page(self, args: list) -> None:
        """Launch Chromium with the given flags and load the client page up
        to the point where the Zoom SDK global exists. Everything before
        the actual meeting join lives here so a flag set that cannot get
        this far can be retried with a different one."""
        self._closing = False
        self._gone_reported = False
        # The window must simply be big enough to hold the gallery, which
        # is sized by CSS and the SDK's viewSizes (640x360), not by this.
        # Believing otherwise cost a night: shrinking the window saved no
        # memory at all, because the SDK kept rendering at its configured
        # size, and it pushed the tiles outside the window where element
        # screenshots could not reach them, which silently ended every
        # face check. Both drivers open the page at 800x600.
        self._browser, self._context, self._page = await self._driver_launch(args)

        # A collapsed browser dies as one piece, and nothing else would
        # notice: a dead page cannot report its own end, and the capture
        # loop swallows per-sweep errors by design, so the session would
        # sit as a zombie that still says "capturing" while suppressing
        # the control plane's replacement bot. Wire the death itself to
        # the lifecycle instead: the manager reaps the session, the bot
        # leaves /bots, and the replacement machinery takes over.
        def _gone(reason: str) -> None:
            if self._closing or self._joining or self._gone_reported:
                return
            self._gone_reported = True
            logger.warning("[BOT] browser gone mid-session: %s", reason)
            handler = getattr(self, "on_lifecycle", None)
            if handler:
                try:
                    asyncio.get_running_loop().create_task(
                        handler("ended", reason))
                except RuntimeError:
                    pass  # no running loop means shutdown is already underway

        self._page.on("crash", lambda _page: _gone("the page crashed"))
        self._browser.on("disconnected",
                         lambda: _gone("the browser process died"))

        # Capture what the page says about itself. Without this a script
        # that throws halfway through is invisible: the error never
        # reaches Python, "load" still fires, and the only symptom is a
        # global that never appears. That cost several rounds of guessing
        # at the Zoom SDK, so the page's own words now come back with the
        # failure.
        self._page.on(
            "pageerror",
            lambda e: self._page_errors.append(str(e)[:400]),
        )
        self._page.on(
            "console",
            lambda m: self._page_console.append(f"[{m.type}] {m.text[:300]}")
            if m.type in ("error", "warning") else None,
        )
        self._page.on(
            "requestfailed",
            lambda r: self._page_errors.append(
                f"request failed: {r.url[:160]} ({r.failure})"
            ),
        )

        # Bridge inbound chat from the page to our async handler.
        async def _on_zoom_chat(payload):
            if self.on_chat:
                await self.on_chat(payload)

        # Bridge meeting lifecycle (the meeting ending, mainly) to Python.
        # Without this the browser sits in a dead meeting indefinitely.
        async def _on_zoom_lifecycle(payload):
            if self.on_lifecycle:
                try:
                    await self.on_lifecycle(
                        str((payload or {}).get("type") or ""),
                        (payload or {}).get("detail"),
                    )
                except Exception as e:  # never let a handler kill the bridge
                    logger.warning("lifecycle handler error: %s", e)

        await self._page.expose_function("onZoomChat", _on_zoom_chat)
        await self._page.expose_function("onZoomLifecycle", _on_zoom_lifecycle)
        # domcontentloaded, not "load", and a long budget. The SDK global is
        # explicitly waited for right below, so full-load adds nothing, and a
        # container already running another bot's Chromium can be slow enough
        # that the default 30 seconds fails the join before it starts.
        await self._page.goto(self.page_url, wait_until="domcontentloaded",
                              timeout=90_000)

        # Wait for the SDK global before touching it. "load" firing only
        # means the page finished; a <script> that failed to fetch does
        # not stop it, so without this the next line throws a bare
        # "ZoomMtgEmbedded is not defined" from inside eval and the real
        # cause (blocked, 404, slow CDN) is nowhere in the message.
        try:
            await self._page.wait_for_function(
                "typeof window.ZoomMtgEmbedded !== 'undefined'", timeout=45000
            )
        except Exception:
            # The page's own errors are the whole diagnosis here. The
            # bundle assigns window.ZoomMtgEmbedded on its last line, so
            # a missing global means it threw before getting there, and
            # only the browser knows why.
            detail = " | ".join(self._page_errors[-4:]) or "(no page errors captured)"
            console = " | ".join(self._page_console[-4:]) or "(no console output)"
            raise RuntimeError(
                f"Zoom Web SDK did not initialise on {self.page_url} "
                "(window.ZoomMtgEmbedded undefined after 45s). "
                f"Page errors: {detail}. Console: {console}"
            ) from None

    async def join(self, *, meeting_number: str, passcode: str, display_name: str,
                   signature: str, sdk_key: str, zak: Optional[str] = None,
                   lookout: bool = False) -> None:
        await self._driver_start()
        self._joining = True
        self._diet_active = bool(lookout)
        try:
            try:
                await self._open_page(self._launch_args(lookout))
            except Exception as e:
                if not lookout:
                    raise
                # The diet must never cost a join. If the collapsed browser
                # cannot start, or its page cannot reach a ready SDK, close
                # it and take the well-trodden flags instead; the memory
                # saving is worth trying for, never worth failing a class.
                logger.warning(
                    "lookout diet browser failed before the join, retrying on "
                    "normal flags: %s", str(e)[:300])
                await self._relaunch_normal()

            camera_face = self._camera_face_decision()
            cfg = {
                "sdkKey": sdk_key,
                "signature": signature,
                "meetingNumber": str(meeting_number),
                "passcode": passcode or "",
                "userName": display_name,
                # Bearer credential: never log this.
                "zak": zak or None,
                # Tiles per gallery page. 25 is Zoom's own per-participant
                # ceiling; tunable down without a rebuild if the memory
                # meter on /healthz ever argues for it.
                "galleryTiles": _gallery_tiles(),
                # Kill switch for the in-page watcher, no rebuild needed.
                "seatWatcher": os.environ.get("BOT_SEAT_WATCHER", "on"),
                # Lookout: thumbnail view, no detector, no face work. The
                # page enforces its side of the bargain from this flag.
                "lookout": bool(lookout),
                # The camera picture: on only when the machine has room
                # for it right now. The page presses Zoom's own button.
                "cameraFace": camera_face[0],
                "cameraFaceReason": camera_face[1],
                # Above this many people the page keeps the picture off:
                # sending video into Zoom costs the browser about 100 MB
                # in a full class (measured 2026-09-06), a nicety that is
                # never worth a frozen box.
                "cameraFaceMaxPeople": _camera_face_max_people(),
            }
            try:
                await self._page.evaluate(
                    """async (cfg) => { await window.zoomJoin(cfg); }""", cfg)
            except Exception as e:
                # The join itself is where a collapsed browser is most
                # fragile: the SDK spins up its media machinery right here.
                # Retry on normal flags only when the browser actually died;
                # a Zoom rejection (bad passcode, waiting room denial)
                # would fail the same way on any flags.
                if not (self._diet_active and _looks_like_browser_death(e)):
                    raise
                logger.warning(
                    "lookout diet browser died during the join, retrying on "
                    "normal flags: %s", str(e)[:300])
                await self._relaunch_normal()
                await self._page.evaluate(
                    """async (cfg) => { await window.zoomJoin(cfg); }""", cfg)
        finally:
            self._joining = False

    async def _relaunch_normal(self) -> None:
        """Tear the diet browser down and reopen the page on normal flags."""
        self._diet_active = False
        await self.close()
        await self._driver_start()
        self._page_errors.clear()
        self._page_console.clear()
        await self._open_page(self._launch_args(False))

    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> bool:
        result = await self._page.evaluate(
            """async ([text, to]) => await window.zoomSendChatConfirmed(text, to, 5000)""",
            [text, to_user_id],
        )
        return bool(result and result.get("echoed"))

    async def list_participants(self) -> List[Participant]:
        users = await self._page.evaluate("""async () => await window.zoomListUsers()""")
        out = []
        for u in users or []:
            out.append(Participant(
                user_id=str(u.get("userId")),
                name=u.get("displayName", ""),
                video_on=bool(u.get("bVideoOn")),
                is_host=bool(u.get("isHost")),
                is_co_host=bool(u.get("isCoHost")),
                is_hold=bool(u.get("isHold")),
            ))
        return out

    async def presence(self) -> PresenceSnapshot:
        snap = await self._page.evaluate("""async () => await window.zoomPresence()""") or {}
        rows = []
        for r in snap.get("rows") or []:
            rows.append(PresenceRow(
                user_id=str(r.get("userId")),
                name=r.get("name") or "",
                joined_at=float(r.get("joinedAt") or 0.0),
                left_at=(None if r.get("leftAt") is None else float(r["leftAt"])),
                present=bool(r.get("present")),
                video_on=bool(r.get("videoOn")),
                video_on_seconds=int(r.get("videoOnSeconds") or 0),
                observed_seconds=int(r.get("observedSeconds") or 0),
            ))
        return PresenceSnapshot(
            at=float(snap.get("at") or 0.0),
            self_user_id=(str(snap["selfUserId"]) if snap.get("selfUserId") else None),
            joined=bool(snap.get("joined")),
            rows=rows,
        )

    async def capture_supported(self) -> bool:
        try:
            return bool(await self._page.evaluate(
                """async () => await window.zoomCaptureSupported()"""))
        except Exception:
            return False

    async def capture_user(self, user_id: str) -> Optional[bytes]:
        """PNG of the user's rendered tile, or None if Zoom has no tile attached.

        The page marks the element the SDK itself bound to this user id
        (node-id attribute), and Playwright screenshots the compositor output
        for it. That works where canvas.toDataURL() returns black on WebGL,
        and it is attribution by Zoom's own binding, never by tile position.
        """
        if not self._page:
            return None

        def note(outcome: str, **extra):
            self._capture_log.append({"at": time.time(), "user": str(user_id),
                                      "outcome": outcome, **extra})
            del self._capture_log[:-12]

        try:
            mark = await self._page.evaluate(
                """async (uid) => await window.zoomMarkUserTile(uid)""", str(user_id)
            ) or {}
            if not mark.get("ok"):
                rendered = mark.get("rendered")
                if rendered is not None:
                    # INFO on purpose: "face never checked" was undiagnosable
                    # from the Render logs the owner actually reads, because
                    # the reason only lived at debug level and in a console
                    # panel nobody opens. Throttled naturally by the per-user
                    # screenshot gap.
                    logger.info("face capture: no tile rendered for %s; rendered right now: %s",
                                user_id, rendered or "none")
                note("no tile rendered for this user",
                     renderedTiles=(rendered if rendered is not None else []))
                return None
            # A tight budget on purpose. Playwright waits for the element to
            # be stable, and a renderer busy compositing several video
            # streams can stay "unstable" for tens of seconds. A tile that
            # cannot settle in 4 seconds was not going to yield a better
            # frame at 10 or 20, and every second spent here stretches the
            # whole sweep past the observation interval.
            shot = await self._page.locator('[data-cap-target="1"]').screenshot(
                type="png", timeout=4000, animations="disabled"
            )
            note("captured", strategy=mark.get("strategy"),
                 size=f"{mark.get('width')}x{mark.get('height')}")
            return shot
        except Exception as e:
            logger.warning("capture_user(%s) screenshot failed: %s", user_id, e)
            note("screenshot failed", error=str(e)[:120])
            return None
        finally:
            try:
                await self._page.evaluate("""async () => await window.zoomUnmarkTile()""")
            except Exception:
                pass

    async def watcher_state(self) -> Optional[dict]:
        if not self._page:
            return None
        try:
            return await self._page.evaluate(
                """async () => await window.zoomWatcherState()""") or None
        except Exception:
            return None

    async def watcher_arm(self) -> Optional[dict]:
        """Ask the page to load and start the detector. Idempotent; the
        caller holds the memory verdict, this only holds the machinery."""
        if not self._page:
            return None
        try:
            return await self._page.evaluate(
                """async () => await window.zoomWatcherArm()""") or None
        except Exception:
            return None

    async def gallery_info(self) -> dict:
        if not self._page:
            return {}
        try:
            return await self._page.evaluate(
                """async () => await window.zoomGalleryInfo()""") or {}
        except Exception:
            return {}

    async def gallery_advance(self) -> dict:
        """Step the SDK's gallery to its next page, wrapping at the end.

        The browser only decodes the tiles on the visible page, so paging is
        what turns a 30 camera class into a bounded, constant memory cost:
        a handful of streams at a time, rotating, instead of all of them.
        """
        if not self._page:
            return {"ok": False}
        try:
            return await self._page.evaluate(
                """async () => await window.zoomGalleryAdvance()""") or {"ok": False}
        except Exception as e:
            logger.debug("gallery advance failed: %s", e)
            return {"ok": False}

    async def stop_video(self) -> Optional[dict]:
        """Ask the page to press Zoom's Stop Video: the camera picture is
        the first thing to give back under memory pressure."""
        if not self._page:
            return None
        try:
            return await self._page.evaluate("async () => await window.zoomStopVideo()")
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    async def shrink_viewport(self) -> None:
        """Emergency decode shed under memory pressure.

        Shrinks the gallery element, not the browser window: the element is
        what the SDK renders into, so this is the only lever that actually
        reduces decoded video while staying in the meeting. One way on
        purpose, since re-inflating near the limit would oscillate straight
        back into the pressure that triggered it.
        """
        if not self._page or getattr(self, "_viewport_shrunk", False):
            return
        try:
            await self._page.evaluate(
                """() => {
                    const root = document.getElementById('zoom-root');
                    if (root) { root.style.width = '320px'; root.style.height = '180px'; }
                }""")
            self._viewport_shrunk = True
            logger.warning("gallery shrunk to 320x180 under memory pressure")
        except Exception as e:
            logger.warning("gallery shrink failed: %s", e)

    async def self_user_id(self) -> Optional[str]:
        try:
            uid = await self._page.evaluate("""async () => await window.zoomSelfUserId()""")
            return str(uid) if uid else None
        except Exception:
            return None

    async def diagnostics(self) -> dict:
        """Raw SDK state, for when reported camera state is disputed."""
        try:
            data = await self._page.evaluate(
                """async () => await window.zoomDiagnostics()""") or {}
        except Exception as e:
            return {"error": f"diagnostics failed: {e}",
                    "dietActive": self._diet_active}
        # Whether the collapsed single-process browser is the one running,
        # or the safety net fell back to the normal one. Answerable from
        # the console instead of by archaeology on the deploy logs.
        data["dietActive"] = self._diet_active
        # The SDK races several Zoom datacenters at join and cancels the
        # losers, and its virtual background engine fails to start headless.
        # Both are one-time startup noise, not faults, and showing them in
        # the panel buried real errors under scary-looking ones.
        def benign(line: str) -> bool:
            return ("/wc/ping/" in line and "ERR_ABORTED" in line) or "init tf fail" in line

        errors = [e for e in self._page_errors if not benign(e)]
        noise = [e for e in self._page_errors if benign(e)]
        data["captureLog"] = self._capture_log[-12:]
        data["page_errors"] = errors[-6:]
        data["startup_noise"] = noise[-4:]
        data["console"] = [c for c in self._page_console if not benign(c)][-8:]
        # The camera picture's report carries Zoom's own complaints about
        # video and the camera (errors and warnings only), so "video never
        # came on" arrives next to the reason Zoom gave.
        cf = data.get("cameraFace")
        if isinstance(cf, dict):
            notes = [n for n in (cf.get("notes") or []) if n]
            for line in self._page_console[-80:]:
                if not line.startswith(("[error]", "[warning]")) or benign(line):
                    continue
                if _CAMERA_WORDS.search(line) and line[:160] not in notes:
                    notes.append(line[:160])
            cf["notes"] = notes[-8:]
        return data

    async def page_screenshot(self) -> Optional[bytes]:
        if not self._page:
            return None
        # One retry: a page busy decoding several video streams can miss a
        # short screenshot window, and the intermittent failure reads as the
        # whole feature flickering.
        for timeout_ms in (12000, 20000):
            try:
                return await self._page.screenshot(type="png", timeout=timeout_ms)
            except Exception as e:
                logger.warning("page screenshot attempt failed: %s", e)
        return None

    async def leave(self) -> None:
        try:
            if self._page:
                await self._page.evaluate("""async () => { await window.zoomLeave(); }""")
        except Exception:
            pass
        await self.close()

    async def close(self) -> None:
        """Tear down the browser. Safe to call twice, and safe to call on a
        half-built client -- which matters, because a join that fails partway
        must not leave a Chromium (and a ghost participant) behind."""
        self._closing = True
        try:
            await self._driver_close()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._page = None
        if self._camera_face_path:
            try:
                os.unlink(self._camera_face_path)
            except OSError:
                pass
            self._camera_face_path = None


class PlaywrightZoomClient(ChromiumZoomClient):
    """The Playwright driver: Chromium behind Playwright's Node.js relay.
    About 60 MB of relay on the small machine, measured live, which is
    why it is no longer the default; kept as the way back."""

    DRIVER = "playwright"

    async def _driver_start(self) -> None:
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()

    async def _driver_launch(self, args: list):
        browser = await self._pw.chromium.launch(headless=self.headless, args=args)
        context = await browser.new_context(viewport={"width": 800, "height": 600})
        page = await context.new_page()
        return browser, context, page

    async def _driver_close(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None


class CdpZoomClient(ChromiumZoomClient):
    """The direct driver: Chromium over its own DevTools pipe (bot/cdp.py).
    No relay program, so the browser gets the memory the relay used."""

    DRIVER = "cdp"

    def __init__(self, page_url: str, headless: bool = True):
        super().__init__(page_url=page_url, headless=headless)
        self._executable: Optional[str] = None

    async def _driver_start(self) -> None:
        from .cdp import find_chromium
        self._executable = find_chromium()
        if not self._executable:
            raise RuntimeError(
                "no Chromium found on this machine (set BOT_CHROMIUM_PATH, or "
                "BOT_BROWSER_DRIVER=playwright to use the relay driver)")

    async def _driver_launch(self, args: list):
        from .cdp import CdpBrowser
        browser = await CdpBrowser(self._executable, args, headless=self.headless).launch()
        page = await browser.new_page(viewport=(800, 600))
        return browser, None, page

    async def _driver_close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass


def browser_driver_name() -> str:
    """Which driver a new client gets: BOT_BROWSER_DRIVER, default playwright.

    The direct driver went into its first class on 2026-09-06 and ran the
    browser heavier than the relay did (Chrome 455 MB for 24 people, the
    box at 98 percent, frozen twice in a morning). Until it is measured at
    or under the relay's numbers in a class, the relay stays the default
    and the direct driver is opt-in: BOT_BROWSER_DRIVER=cdp."""
    raw = os.environ.get("BOT_BROWSER_DRIVER", "playwright").strip().lower()
    return "cdp" if raw == "cdp" else "playwright"


def build_meeting_client(page_url: str, headless: bool) -> MeetingClient:
    cls = PlaywrightZoomClient if browser_driver_name() == "playwright" else CdpZoomClient
    return cls(page_url=page_url, headless=headless)
