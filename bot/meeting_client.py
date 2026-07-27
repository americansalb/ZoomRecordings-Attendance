"""
Meeting client abstraction.

The capture loop and bot manager talk to a meeting through this interface so the
orchestration is testable without a real meeting:

  - MeetingClient: the interface.
  - FakeMeetingClient: in-memory, for tests.
  - PlaywrightZoomClient: drives a headless Chromium running the Zoom Web SDK
    (see static/zoom_client.html + zoom_client.js). Built correct-by-construction;
    requires Playwright + Zoom SDK creds to actually run.

Identity note: capture_user(user_id) renders *that user's own* video stream to an
off-screen canvas and grabs it. Attribution is therefore by Zoom user id, never
by tile position.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

ChatHandler = Callable[[dict], Awaitable[None]]


@dataclass
class Participant:
    user_id: str
    name: str
    video_on: bool = False
    is_host: bool = False


class MeetingClient(ABC):
    on_chat: Optional[ChatHandler] = None

    @abstractmethod
    async def join(self, *, meeting_number: str, passcode: str, display_name: str,
                   signature: str, sdk_key: str, zak: Optional[str] = None) -> None: ...

    @abstractmethod
    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> None: ...

    @abstractmethod
    async def list_participants(self) -> List[Participant]: ...

    @abstractmethod
    async def capture_user(self, user_id: str) -> Optional[bytes]:
        """Render the user's video to a canvas and return PNG bytes (None if off)."""

    @abstractmethod
    async def leave(self) -> None: ...


class FakeMeetingClient(MeetingClient):
    """In-memory meeting for tests."""

    def __init__(self, participants: Optional[List[Participant]] = None,
                 frames: Optional[dict] = None):
        self._participants = participants or []
        self._frames = frames or {}   # user_id -> bytes
        self.sent_chats: list[dict] = []
        self.joined = False

    async def join(self, **kwargs) -> None:
        self.joined = True

    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> None:
        self.sent_chats.append({"text": text, "to": to_user_id})

    async def list_participants(self) -> List[Participant]:
        return list(self._participants)

    async def capture_user(self, user_id: str) -> Optional[bytes]:
        return self._frames.get(user_id)

    async def leave(self) -> None:
        self.joined = False

    async def inject_chat(self, event: dict) -> None:
        if self.on_chat:
            await self.on_chat(event)


class PlaywrightZoomClient(MeetingClient):
    """Drives the Zoom Web SDK inside a headless Chromium via Playwright."""

    def __init__(self, page_url: str, headless: bool = True):
        self.page_url = page_url
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    async def join(self, *, meeting_number: str, passcode: str, display_name: str,
                   signature: str, sdk_key: str, zak: Optional[str] = None) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        # Flags: fake media so Chromium grants mic/cam without hardware, and the
        # WebRTC bits the Web SDK needs in a container.
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--no-sandbox",
            ],
        )
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()

        # Capture what the page says about itself. Without this a script
        # that throws halfway through is invisible: the error never
        # reaches Python, "load" still fires, and the only symptom is a
        # global that never appears. That cost several rounds of guessing
        # at the Zoom SDK, so the page's own words now come back with the
        # failure.
        self._page_errors: List[str] = []
        self._page_console: List[str] = []
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

        await self._page.expose_function("onZoomChat", _on_zoom_chat)
        await self._page.goto(self.page_url, wait_until="load")

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

        await self._page.evaluate(
            """async (cfg) => { await window.zoomJoin(cfg); }""",
            {
                "sdkKey": sdk_key,
                "signature": signature,
                "meetingNumber": str(meeting_number),
                "passcode": passcode or "",
                "userName": display_name,
                # Bearer credential: never log this.
                "zak": zak or None,
            },
        )

    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> None:
        await self._page.evaluate(
            """async ([text, to]) => { await window.zoomSendChat(text, to); }""",
            [text, to_user_id],
        )

    async def list_participants(self) -> List[Participant]:
        users = await self._page.evaluate("""async () => await window.zoomListUsers()""")
        out = []
        for u in users or []:
            out.append(Participant(
                user_id=str(u.get("userId")),
                name=u.get("displayName", ""),
                video_on=bool(u.get("bVideoOn")),
                is_host=bool(u.get("isHost")),
            ))
        return out

    async def capture_user(self, user_id: str) -> Optional[bytes]:
        data_url = await self._page.evaluate(
            """async (uid) => await window.zoomCaptureUser(uid)""", user_id
        )
        if not data_url:
            return None
        try:
            b64 = data_url.split(",", 1)[1]
            return base64.b64decode(b64)
        except Exception as e:
            logger.warning("capture_user decode failed for %s: %s", user_id, e)
            return None

    async def leave(self) -> None:
        try:
            if self._page:
                await self._page.evaluate("""async () => { await window.zoomLeave(); }""")
        except Exception:
            pass
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


def build_meeting_client(page_url: str, headless: bool) -> MeetingClient:
    return PlaywrightZoomClient(page_url=page_url, headless=headless)
