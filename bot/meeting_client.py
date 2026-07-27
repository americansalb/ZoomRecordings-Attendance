"""
Meeting client abstraction.

The capture loop and bot manager talk to a meeting through this interface so the
orchestration is testable without a real meeting:

  - MeetingClient: the interface.
  - FakeMeetingClient: in-memory, for tests.
  - PlaywrightZoomClient: drives a headless Chromium running the Zoom Web SDK
    (see static/zoom_client.html + zoom_client.js).

Identity note: attendance is attributed by Zoom user id, never by tile position.
`presence()` returns the per-user ledger the browser page maintains from Zoom's
own user-added/user-removed/user-updated events.

Capture note: per-user frame capture is not available on the Component View SDK
(no getMediaStream on the meeting client, in any released version). capture_user
returns None and capture_supported() is False; see zoom_client.js for the full
explanation. video_on from Zoom's per-user state remains authoritative.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

ChatHandler = Callable[[dict], Awaitable[None]]
LifecycleHandler = Callable[[str, Optional[str]], Awaitable[None]]


@dataclass
class Participant:
    user_id: str
    name: str
    video_on: bool = False
    is_host: bool = False
    is_co_host: bool = False


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
    on_chat: Optional[ChatHandler] = None
    on_lifecycle: Optional[LifecycleHandler] = None

    @abstractmethod
    async def join(self, *, meeting_number: str, passcode: str, display_name: str,
                   signature: str, sdk_key: str, zak: Optional[str] = None) -> None: ...

    @abstractmethod
    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> None: ...

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

    async def send_chat(self, text: str, to_user_id: Optional[str] = None) -> None:
        self.sent_chats.append({"text": text, "to": to_user_id})

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


class PlaywrightZoomClient(MeetingClient):
    """Drives the Zoom Web SDK inside a headless Chromium via Playwright."""

    def __init__(self, page_url: str, headless: bool = True):
        self.page_url = page_url
        self.headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._page_errors: List[str] = []
        self._page_console: List[str] = []

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
                is_co_host=bool(u.get("isCoHost")),
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
            return {"error": f"diagnostics failed: {e}"}
        data["page_errors"] = self._page_errors[-6:]
        data["console"] = self._page_console[-8:]
        return data

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
        for closer in (self._context, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._page = None
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None


def build_meeting_client(page_url: str, headless: bool) -> MeetingClient:
    return PlaywrightZoomClient(page_url=page_url, headless=headless)
