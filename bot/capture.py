"""
Per-student capture loop.

Every `interval_seconds`, for each participant:
  1. Read video_on from Zoom's per-user state (the SDK tells us, authoritatively).
  2. If on, render *that user's* video to a canvas and grab a PNG.
  3. face_present? (OpenCV) -- the cross-check signal.
  4. If storing images, upload to Drive (per-session folder).
  5. Report one manifest row to the backend, attributed by Zoom user id + name.

Attribution never depends on tile position; identity is the Zoom user id, and the
name is recorded alongside as a failsafe. The face/video flags cross-check each
other so a camera-off or no-face state is visible without losing who it was.
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
    bot_name: str        # so we never screenshot the bot itself


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "unknown")).strip("-") or "unknown"


class CaptureLoop:
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
        self.interval_seconds = max(30, int(interval_seconds or 300))
        self.store_images = store_images
        self.face_detector = face_detector
        self._stop = asyncio.Event()

    async def run_once(self, ctx: CaptureContext) -> List[dict]:
        rows: List[dict] = []
        participants = await self.client.list_participants()
        folder = f"LiveTutor {ctx.session_label}".strip()

        for p in participants:
            if p.name and p.name == ctx.bot_name:
                continue  # don't snapshot ourselves

            video_on = bool(p.video_on)
            data: Optional[bytes] = None
            if video_on:
                try:
                    data = await self.client.capture_user(p.user_id)
                except Exception as e:
                    logger.warning("capture_user(%s) failed: %s", p.user_id, e)
                    data = None

            face = bool(self.face_detector(data)) if data else False

            stored = False
            image_url = None
            drive_file_id = None
            if self.store_images and data:
                ts = time.strftime("%Y-%m-%d_%H-%M-%S")
                filename = f"{_safe(ctx.session_label)}_{_safe(p.name)}_{ts}.png"
                drive_file_id, image_url = await self.storage.upload(
                    data=data, filename=filename, session_folder=folder
                )
                stored = bool(drive_file_id or image_url)

            row = {
                "session_ref": ctx.session_ref,
                "runtime_id": ctx.runtime_id,
                "participant_id": p.user_id,
                "participant_name": p.name,
                "registrant_id": None,
                "captured_at": time.time(),
                "video_on": video_on,
                "face_present": face,
                "stored": stored,
                "image_url": image_url,
                "drive_file_id": drive_file_id,
            }
            await self.backend.post_screenshot(row)
            rows.append(row)

        return rows

    async def run(self, ctx: CaptureContext) -> None:
        logger.info("[CAPTURE] loop started for session %s every %ss",
                    ctx.session_ref, self.interval_seconds)
        while not self._stop.is_set():
            try:
                await self.run_once(ctx)
            except Exception as e:  # keep the loop alive across transient errors
                logger.warning("[CAPTURE] run_once error: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
        logger.info("[CAPTURE] loop stopped for session %s", ctx.session_ref)

    def stop(self) -> None:
        self._stop.set()
