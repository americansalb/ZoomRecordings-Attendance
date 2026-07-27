"""
Per-student attendance loop.

Every `interval_seconds`, for each participant:
  1. Read the presence ledger the browser page maintains from Zoom's own
     user-added/user-removed/user-updated events: who is here, since when, and
     how long their camera has been on.
  2. Read video_on from Zoom's per-user state (authoritative).
  3. If a frame is available, run the face check (OpenCV) as a cross-check.
  4. If storing images, upload to Drive (per-session folder).
  5. Report one attendance row and one manifest row per student to the backend,
     attributed by Zoom user id + name.

Attribution never depends on tile position; identity is the Zoom user id, and
the name is recorded alongside as a failsafe.

Two things this loop deliberately does NOT do:

  - It does not require frame capture. Per-user frames are unavailable on the
    Component View SDK (see meeting_client.py), so `video_on` and presence
    duration carry attendance on their own and `face_present` is recorded as a
    cross-check only when a frame actually exists.
  - It does not depend on the screenshot toggle. Attendance is reported on
    every tick regardless; `store_images` only decides whether pixels are kept.
    Attendance that switches itself off when a privacy setting is enabled is
    not attendance.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from .face import face_present as default_face_detector
from .meeting_client import MeetingClient

logger = logging.getLogger(__name__)


@dataclass
class CaptureContext:
    runtime_id: str
    session_ref: str
    meeting_id: str
    session_label: str   # used for the Drive folder name
    bot_name: str        # fallback identity for skipping ourselves


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "unknown")).strip("-") or "unknown"


class CaptureLoop:
    # Floor on the cadence. A tick is two cheap page reads plus, when frames
    # exist, a face check per student, so it is not free -- but it is far
    # lighter than it was when every tick tried to render video per user.
    MIN_INTERVAL_SECONDS = 10

    def __init__(
        self,
        client: MeetingClient,
        backend,
        storage,
        *,
        interval_seconds: int,
        store_images: bool,
        face_detector: Callable[[bytes], bool] = default_face_detector,
    ):
        self.client = client
        self.backend = backend
        self.storage = storage
        self.interval_seconds = self._clamp(interval_seconds)
        self.store_images = store_images
        self.face_detector = face_detector
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        # Serialises the scheduled sweep against an operator-triggered one, so
        # "take attendance now" cannot interleave with a tick already running
        # and report a half-built ledger.
        self._sweep_lock = asyncio.Lock()
        self._self_id: Optional[str] = None

    @classmethod
    def _clamp(cls, seconds) -> int:
        return max(cls.MIN_INTERVAL_SECONDS, int(seconds or 300))

    def set_interval(self, seconds: int) -> int:
        """Change the cadence of a loop that is already running.

        Capture config used to reach the bot only at join time, so changing the
        interval did nothing until the bot was dismissed and re-summoned.
        """
        self.interval_seconds = self._clamp(seconds)
        self._wake.set()          # re-arm the sleep against the new interval
        return self.interval_seconds

    def trigger_now(self) -> None:
        """Ask the loop to sweep immediately rather than finish its sleep."""
        self._wake.set()

    def _is_self(self, user_id: str, name: str, ctx: CaptureContext) -> bool:
        """Never record the bot as a student.

        The id check alone is not enough. The SDK's current-user id and the id
        the roster carries for the bot can differ (observed live: getCurrentUser
        reported 16788480 while the attendee list row said 16789504), and when
        they do the bot records itself as an attendee, appears on the report,
        and queues a camera message to itself. So the display name is always
        checked as well. The bot's name is ours to choose, so a student
        colliding with it is avoidable; the bot messaging itself is not.
        """
        if self._self_id and str(user_id) == str(self._self_id):
            return True
        return bool(name) and name == ctx.bot_name

    async def run_once(self, ctx: CaptureContext) -> List[dict]:
        async with self._sweep_lock:
            return await self._sweep(ctx)

    async def _sweep(self, ctx: CaptureContext) -> List[dict]:
        rows: List[dict] = []

        if self._self_id is None:
            try:
                self._self_id = await self.client.self_user_id()
            except Exception:
                self._self_id = None

        snapshot = await self.client.presence()
        if snapshot.self_user_id:
            self._self_id = snapshot.self_user_id

        # video_on comes from the live roster; the ledger carries the durations.
        participants = {p.user_id: p for p in await self.client.list_participants()}
        folder = f"LiveTutor {ctx.session_label}".strip()
        captured_at = snapshot.at or time.time()

        for row in snapshot.rows:
            if self._is_self(row.user_id, row.name, ctx):
                continue

            p = participants.get(row.user_id)
            video_on = bool(p.video_on) if p else bool(row.video_on)
            # Role has to be read live rather than cached at join, because
            # co-host is granted mid-meeting. The control plane needs it to
            # leave whoever is running the class alone.
            is_host = bool(p.is_host) if p else False
            is_cohost = bool(p.is_co_host) if p else False

            data: Optional[bytes] = None
            if video_on:
                try:
                    data = await self.client.capture_user(row.user_id)
                except Exception as e:
                    logger.warning("capture_user(%s) failed: %s", row.user_id, e)
                    data = None

            # None means "never looked", which is not the same claim as False,
            # "looked and saw nobody". Sending False for an unexamined frame
            # made every tick read downstream as a completed face check, so a
            # report could show 0 of 27 checks failing when no check ever ran.
            # Per-user frames are unavailable on this SDK, so today this is
            # always None; it must stay honest if that ever changes.
            face: Optional[bool] = None
            if data:
                face = False
                # OpenCV decode + Haar cascade is CPU-bound and was running
                # inline on the event loop, stalling chat and the other
                # participants' captures for the length of every detection.
                try:
                    face = bool(await asyncio.to_thread(self.face_detector, data))
                except Exception as e:
                    logger.warning("face check failed for %s: %s", row.user_id, e)

            stored = False
            image_url = None
            drive_file_id = None
            if self.store_images and data:
                ts = time.strftime("%Y-%m-%d_%H-%M-%S")
                filename = f"{_safe(ctx.session_label)}_{_safe(row.name)}_{ts}.png"
                drive_file_id, image_url = await self.storage.upload(
                    data=data, filename=filename, session_folder=folder
                )
                stored = bool(drive_file_id or image_url)

            attendance = {
                "session_ref": ctx.session_ref,
                "runtime_id": ctx.runtime_id,
                "participant_id": row.user_id,
                "participant_name": row.name,
                "registrant_id": None,
                "observed_at": captured_at,
                "joined_at": row.joined_at,
                "left_at": row.left_at,
                "present": row.present,
                "video_on": video_on,
                "video_on_seconds": row.video_on_seconds,
                "observed_seconds": row.observed_seconds,
                "face_present": face,
                "face_checked": data is not None,
                "is_host": is_host,
                "is_cohost": is_cohost,
            }
            await self.backend.post_attendance(attendance)

            manifest = {
                "session_ref": ctx.session_ref,
                "runtime_id": ctx.runtime_id,
                "participant_id": row.user_id,
                "participant_name": row.name,
                "registrant_id": None,
                "captured_at": captured_at,
                "video_on": video_on,
                "face_present": face,
                "stored": stored,
                "image_url": image_url,
                "drive_file_id": drive_file_id,
                "is_host": is_host,
                "is_cohost": is_cohost,
            }
            await self.backend.post_screenshot(manifest)
            rows.append(attendance)

        return rows

    async def run(self, ctx: CaptureContext) -> None:
        logger.info("[CAPTURE] attendance loop started for session %s every %ss",
                    ctx.session_ref, self.interval_seconds)
        try:
            if not await self.client.capture_supported():
                logger.info(
                    "[CAPTURE] per-user frame capture unavailable on this SDK; "
                    "recording presence and camera state only")
        except Exception:
            pass
        while not self._stop.is_set():
            try:
                await self.run_once(ctx)
            except Exception as e:  # keep the loop alive across transient errors
                logger.warning("[CAPTURE] run_once error: %s", e)
            await self._sleep_until_next()
        logger.info("[CAPTURE] attendance loop stopped for session %s", ctx.session_ref)

    async def _sleep_until_next(self) -> None:
        """Wait out the interval, but wake early on stop or on a nudge.

        A plain sleep meant an operator who wanted attendance right now, or who
        shortened the interval, had to wait out the old one first.
        """
        waiters = [asyncio.ensure_future(self._stop.wait()),
                   asyncio.ensure_future(self._wake.wait())]
        try:
            await asyncio.wait(waiters, timeout=self.interval_seconds,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()          # do not sit out the interval before exiting
