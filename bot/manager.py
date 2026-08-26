"""
BotManager: the orchestration layer behind the TUTOR_BOT.md HTTP contract.

Tracks one meeting client per runtime_id, wires inbound chat and meeting
lifecycle to the backend webhook, and runs a per-student attendance loop.
Meeting client + storage are injected via factories so this is testable with
fakes.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .capture import CaptureContext, CaptureLoop
from .meeting_client import MeetingClient
from .signature import meeting_sdk_signature

logger = logging.getLogger(__name__)


@dataclass
class BotSession:
    runtime_id: str
    meeting_id: str
    session_ref: str
    display_name: str
    client: MeetingClient
    loop: Optional[CaptureLoop] = None
    task: Optional[asyncio.Task] = field(default=None)
    ctx: Optional[CaptureContext] = None
    # (recipient, text) -> monotonic time of the last DELIVERED send.
    # Delivery idempotence lives here, see BotManager.send.
    recent_sends: Dict[Tuple[Optional[str], str], float] = field(default_factory=dict)


def _parse_lookout(capture: Dict[str, Any]) -> bool:
    """Whether this session is a lookout (no video work of any kind).

    Absent means: lookout unless the caller asked for pixels. Callers that
    predate the flag (the tutor backend) send capture.enabled for their
    screenshot pipeline and must keep the watcher they always had; every
    caller that knows about lookout sends it explicitly. JSON null is
    treated the same as absent, and a string "false" means false: bool()
    on either would quietly flip the meaning.
    """
    raw = capture.get("lookout")
    if raw is None:
        return not bool(capture.get("enabled"))
    if isinstance(raw, str):
        return raw.strip().lower() not in ("false", "0", "no", "off")
    return bool(raw)


def _passcode_from_join_url(join_url: Optional[str]) -> str:
    if not join_url:
        return ""
    try:
        qs = parse_qs(urlparse(join_url).query)
        return (qs.get("pwd") or [""])[0]
    except Exception:
        return ""


class BotManager:
    def __init__(
        self,
        config,
        backend,
        *,
        client_factory: Callable[..., MeetingClient],
        storage_factory: Callable[[bool, Optional[str]], Any],
    ):
        self.config = config
        self.backend = backend
        self.client_factory = client_factory
        self.storage_factory = storage_factory
        self._sessions: Dict[str, BotSession] = {}
        # One Chromium launch at a time. Overlapping joins each spawned a
        # browser on a machine that can barely run one, and the pile-up
        # thrashed the container until launches themselves timed out at 180
        # seconds and the whole API went dark.
        self._join_lock = asyncio.Lock()

    async def join(self, payload: Dict[str, Any]) -> str:
        meeting_id = str(payload.get("meeting_id") or "").strip()
        if not meeting_id:
            raise ValueError("meeting_id is required")
        if not (self.config.sdk_key and self.config.sdk_secret):
            raise RuntimeError("ZOOM_MEETING_SDK_KEY/ZOOM_MEETING_SDK_SECRET not configured")
        if self._join_lock.locked():
            raise RuntimeError(
                "another bot is already joining on this container; "
                "wait for that join to finish, then send again")
        async with self._join_lock:
            return await self._join_inner(payload, meeting_id)

    async def _join_inner(self, payload: Dict[str, Any], meeting_id: str) -> str:

        session_ref = str(payload.get("session_ref") or "")
        display_name = payload.get("display_name") or "AALB Assistant"
        runtime_id = f"bot_{uuid.uuid4().hex[:16]}"

        client = self.client_factory(page_url=self._page_url(), headless=self.config.headless)

        async def _on_chat(raw: Dict[str, Any]) -> None:
            sender = raw.get("sender") or {}
            await self.backend.post_event({
                "type": "chat",
                "session_ref": session_ref,
                "runtime_id": runtime_id,
                "channel": "dm" if raw.get("isPrivate") else "public",
                "participant_id": str(sender.get("userId") or ""),
                "participant_name": sender.get("name") or "",
                "text": raw.get("message") or raw.get("text") or "",
            })

        async def _on_lifecycle(event_type: str, detail: Optional[str]) -> None:
            # The meeting ending is the important one: without it the headless
            # browser sits in a dead meeting until the process is restarted,
            # and the backend keeps the session marked active forever.
            if event_type in ("ended", "left"):
                await self.backend.post_event({
                    "type": "left",
                    "session_ref": session_ref,
                    "runtime_id": runtime_id,
                    "detail": detail,
                })
                asyncio.create_task(self._reap(runtime_id))

        client.on_chat = _on_chat
        client.on_lifecycle = _on_lifecycle

        # Role must agree with how we are authenticating. A ZAK joins as
        # a specific, authenticated Zoom user (ours, set as alternative
        # host on the meeting), which is a role-1 join. Signing role 0
        # while presenting a ZAK is a contradiction and Zoom rejects it.
        # Without a ZAK we are an anonymous participant, which is role 0.
        #
        # `role` is overridable because the pairing is not absolute: a ZAK for
        # a user who is neither host nor alternative host must still sign
        # role 0, and signing role 1 there is rejected.
        # Lookout: presence, camera state, and chat only, no video work of
        # any kind. The default, because it is the mode that survives a
        # room of any size on this machine; watching video is the opt-in.
        # Parsed before the join because the browser page needs to know at
        # init time how much video it is allowed to render.
        capture = payload.get("capture") or {}
        lookout = _parse_lookout(capture)

        zak = payload.get("zak") or None
        role = payload.get("role")
        role = int(role) if role is not None else (1 if zak else 0)
        signature = meeting_sdk_signature(
            self.config.sdk_key,
            self.config.sdk_secret,
            meeting_id,
            role=role,
        )

        try:
            await client.join(
                meeting_number=meeting_id,
                passcode=(
                    payload.get("passcode")
                    or _passcode_from_join_url(payload.get("join_url"))
                ),
                display_name=display_name,
                signature=signature,
                sdk_key=self.config.sdk_key,
                zak=zak,
                lookout=lookout,
            )
        except Exception as e:
            # A join that raises partway can still have left a browser in the
            # meeting -- the SDK connects before the bookkeeping that follows.
            # Without this the failure leaks a Chromium AND parks a silent,
            # undismissable participant in the class, because nothing upstream
            # ever learns a runtime_id to send a leave to.
            logger.warning("[BOT] join failed for meeting %s, tearing down: %s", meeting_id, e)
            await self._force_close(client)
            # Name the most common real cause. A small instance cannot run
            # two Chromiums, so a join attempted while another bot is live
            # fails on timeouts that look like network problems.
            others = len(self._sessions)
            hint = (
                f" ({others} other bot{'s are' if others != 1 else ' is'} live in this "
                "container; a small instance usually cannot run two browsers, "
                "recall the other bot first)"
            ) if others else ""
            await self.backend.post_event({
                "type": "error",
                "session_ref": session_ref,
                "runtime_id": runtime_id,
                "error": (str(e) + hint)[:500],
            })
            raise RuntimeError(f"{e}{hint}") from e

        session = BotSession(runtime_id, meeting_id, session_ref, display_name, client)
        self._sessions[runtime_id] = session

        await self.backend.post_event({
            "type": "joined",
            "session_ref": session_ref,
            "runtime_id": runtime_id,
        })

        if payload.get("announce") and payload.get("announcement"):
            try:
                await client.send_chat(payload["announcement"])
            except Exception as e:
                logger.warning("announcement failed: %s", e)

        # The attendance loop always runs. `capture.enabled` decides whether
        # frames are grabbed and kept, not whether attendance is taken -- the
        # two were previously the same switch, so a privacy-conscious operator
        # who left screenshots off got no attendance record at all.
        store_images = (not lookout
                        and bool(capture.get("enabled"))
                        and bool(capture.get("store_images", True)))
        # Whole-room pictures on their own clock, independent of the
        # per-person frame switch: a session can keep individual frames off
        # and still ask for room evidence. Real storage is needed if either
        # wants to keep pixels. A lookout keeps no pixels at all.
        room_snapshot_seconds = (
            0 if lookout else int(capture.get("room_snapshot_seconds", 0) or 0))
        storage = self.storage_factory(
            store_images or room_snapshot_seconds > 0, self.config.drive_folder_id)
        loop = CaptureLoop(
            client, self.backend, storage,
            interval_seconds=int(capture.get("interval_seconds", 300)),
            store_images=store_images,
            room_snapshot_seconds=room_snapshot_seconds,
            lookout=lookout,
        )
        ctx = CaptureContext(
            runtime_id=runtime_id,
            session_ref=session_ref,
            meeting_id=meeting_id,
            session_label=session_ref or meeting_id,
            bot_name=display_name,
        )
        session.loop = loop
        session.ctx = ctx
        session.task = asyncio.create_task(loop.run(ctx))

        logger.info("[BOT] joined meeting %s as %s (lookout=%s, store_images=%s)",
                    meeting_id, runtime_id, lookout, store_images)
        return runtime_id

    async def leave(self, runtime_id: str) -> None:
        session = self._sessions.pop(runtime_id, None)
        if not session:
            return
        await self._shutdown_session(session)

    async def _reap(self, runtime_id: str) -> None:
        """Tear down after the meeting ended on Zoom's side."""
        session = self._sessions.pop(runtime_id, None)
        if not session:
            return
        logger.info("[BOT] meeting ended, reaping %s", runtime_id)
        try:
            await self._shutdown_session(session)
        except Exception as e:
            # This runs as a detached task, so an exception here would surface
            # only as "Task exception was never retrieved" and the browser
            # would stay up.
            logger.warning("[BOT] reap of %s failed: %s", runtime_id, e)

    async def _shutdown_session(self, session: BotSession) -> None:
        if session.loop:
            session.loop.stop()
        if session.task:
            # Stopping the capture loop is best-effort. Whatever happens to the
            # task -- it times out, it was already cancelled, it belongs to a
            # loop that is going away -- must not stop us from closing the
            # browser below, which is the part that actually removes the bot
            # from the meeting.
            try:
                await asyncio.wait_for(session.task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                session.task.cancel()
            except Exception as e:
                logger.warning("capture task shutdown error: %s", e)
                session.task.cancel()
        try:
            await session.client.leave()
        except Exception as e:
            logger.warning("leave error: %s", e)
            await self._force_close(session.client)

    @staticmethod
    async def _force_close(client: MeetingClient) -> None:
        closer = getattr(client, "close", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception as e:
            logger.warning("browser teardown failed: %s", e)

    def list_sessions(self) -> list:
        """The bots actually running in this process.

        The control plane cannot infer this from its own database: it records
        a join optimistically, and a bot that crashed, was recalled out of
        band, or died with its container sends nothing. Without an
        authoritative read, a stale row shows "bot in room" over an empty
        meeting indefinitely.
        """
        return [
            {
                "runtime_id": s.runtime_id,
                "meeting_id": s.meeting_id,
                "session_ref": s.session_ref,
                "display_name": s.display_name,
                "capturing": bool(s.task and not s.task.done()),
                "lookout": bool(s.loop.lookout) if s.loop else False,
            }
            for s in self._sessions.values()
        ]

    def _require(self, runtime_id: str) -> BotSession:
        session = self._sessions.get(runtime_id)
        if not session:
            raise KeyError(f"unknown runtime_id {runtime_id}")
        return session

    async def capture_now(self, runtime_id: str) -> int:
        """Run one attendance sweep immediately. Returns the rows recorded.

        Awaited rather than fired off, so the caller gets a real count back and
        the operator learns straight away whether the bot can actually see the
        room, instead of being told "triggered" and having to go and look.
        """
        session = self._require(runtime_id)
        ctx = session.ctx
        if not session.loop or not ctx:
            raise RuntimeError("no attendance loop for this session")
        rows = await session.loop.run_once(ctx)
        logger.info("[BOT] manual attendance sweep for %s recorded %d row(s)",
                    runtime_id, len(rows))
        return len(rows)

    def set_capture_config(self, runtime_id: str, *,
                           interval_seconds: Optional[int] = None,
                           store_images: Optional[bool] = None,
                           room_snapshot_seconds: Optional[int] = None) -> Dict[str, Any]:
        """Reconfigure a running loop, so a settings change does not require
        dismissing and re-summoning the bot."""
        session = self._require(runtime_id)
        if not session.loop:
            raise RuntimeError("no attendance loop for this session")
        if interval_seconds is not None:
            session.loop.set_interval(interval_seconds)
        # A lookout keeps no pixels, and that is a join-time promise: the
        # page never rendered enough video to make pixels worth keeping,
        # so turning these on mid-session would only pretend to work. Say
        # so in the response instead of silently succeeding: the caller
        # writes its own record first and would otherwise store a setting
        # this bot will never honor.
        declined = None
        if session.loop.lookout and (
                bool(store_images) or int(room_snapshot_seconds or 0) > 0):
            declined = ("lookout sessions keep no pixels; send the bot with "
                        "video watching on to store frames or room pictures")
        if session.loop.lookout:
            store_images = None
            room_snapshot_seconds = None
        if store_images is not None:
            session.loop.store_images = bool(store_images)
        if room_snapshot_seconds is not None:
            # Turning room pictures on mid-session needs storage that
            # actually keeps pixels; a session that joined with everything
            # off was built with the throwaway store.
            session.loop.room_snapshot_seconds = max(0, int(room_snapshot_seconds))
            if (session.loop.room_snapshot_seconds > 0
                    and not session.loop.storage.stores_images):
                session.loop.storage = self.storage_factory(
                    True, self.config.drive_folder_id)
        resp = {
            "interval_seconds": session.loop.interval_seconds,
            "store_images": session.loop.store_images,
            "room_snapshot_seconds": session.loop.room_snapshot_seconds,
            "lookout": session.loop.lookout,
        }
        if declined:
            resp["note"] = declined
        return resp

    # How long a delivered message shields against an identical resend.
    # Long enough to cover the control plane's retry pass, short enough
    # that a genuinely new identical message later in class still goes out.
    DEDUPE_WINDOW_SECONDS = 600

    async def send(self, runtime_id: str, channel: str, text: str,
                   to_participant_id: Optional[str] = None) -> bool:
        """Deliver a chat message exactly once.

        The control plane can only see its own HTTP call, not the meeting.
        When this container answers slowly, the caller times out, records a
        failure, and sends the same message again on its next pass, while
        the first copy was in fact delivered. That is how one student got a
        stack of identical reminders all numbered #1. Delivery is therefore
        idempotent here, at the only place that knows what was actually
        delivered: an identical (recipient, text) within the window is
        acknowledged as delivered without sending, which also lets the
        caller repair its record of the copy it wrongly wrote off.
        """
        session = self._sessions.get(runtime_id)
        if not session:
            raise KeyError(f"unknown runtime_id {runtime_id}")
        to = to_participant_id if channel == "dm" else None
        key = (to, text)
        now = time.monotonic()
        last = session.recent_sends.get(key)
        if last is not None and (now - last) < self.DEDUPE_WINDOW_SECONDS:
            logger.info("[BOT] duplicate send suppressed (same text to %s, delivered %ds ago)",
                        to or "everyone", int(now - last))
            return True
        delivered = await session.client.send_chat(text, to_user_id=to)
        if delivered:
            session.recent_sends[key] = now
            if len(session.recent_sends) > 200:
                cutoff = now - self.DEDUPE_WINDOW_SECONDS
                session.recent_sends = {
                    k: v for k, v in session.recent_sends.items() if v > cutoff
                }
        return delivered

    def _page_url(self) -> str:
        return f"{self.config.public_base_url}/static/zoom_client.html"

    async def shutdown(self) -> None:
        for rid in list(self._sessions):
            await self.leave(rid)
