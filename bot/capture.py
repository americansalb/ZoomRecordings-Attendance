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
    # Floor on the cadence: one second, the owner's requirement. A tick is
    # two cheap page reads plus the watcher's already-computed face states;
    # the expensive parts are throttled independently below (screenshots by
    # POLAROID_MIN_GAP_SECONDS, paging by its own trigger), so a one second
    # notebook does not mean one second screenshot storms.
    MIN_INTERVAL_SECONDS = 1

    # Fallback screenshots for people the watcher cannot see are never
    # taken more often than this per person, whatever the observation
    # pace. Screenshots are the one genuinely heavy step left in a sweep.
    POLAROID_MIN_GAP_SECONDS = 10

    # Gallery pages are never flipped faster than this. A freshly shown
    # tile needs a second or two before it displays real video, so
    # flipping every sweep at a one second pace would photograph loading
    # tiles forever and churn stream subscriptions.
    PAGE_FLIP_MIN_GAP_SECONDS = 10

    # Face checks are the expensive part of a sweep: each one renders a
    # tile, screenshots it and runs the detector, while presence and camera
    # state for the whole room come from one roster read. Capping the tile
    # work per sweep keeps the sweep near the observation interval no
    # matter how many cameras are on; the checks rotate, so over a few
    # sweeps everyone with a camera on still gets sampled.
    FACE_CHECKS_PER_SWEEP = 4

    # The memory pressure valve. What kills the container is video decode,
    # never attendance: the roster read costs almost nothing. So as the
    # container nears its memory limit, face capture work pauses and
    # attendance continues, and near the very edge the window shrinks,
    # which is the one way to hand decoded video memory back while staying
    # in the meeting. Hysteresis so it does not flap. Dying mid-class
    # takes the rest of the class record with it; degrading does not.
    MEM_SOFT_LIMIT = 0.85
    MEM_HARD_LIMIT = 0.92
    MEM_RESUME_BELOW = 0.70
    WATCHDOG_SECONDS = 2

    # A watcher reading older than this is not evidence about right now.
    WATCHER_FRESH_MS = 5000
    MEM_CURRENT_PATHS = ("/sys/fs/cgroup/memory.current",
                         "/sys/fs/cgroup/memory/memory.usage_in_bytes")
    MEM_MAX_PATHS = ("/sys/fs/cgroup/memory.max",
                     "/sys/fs/cgroup/memory/memory.limit_in_bytes")

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
        self._rearm_only = False
        # Rotation cursor for the per-sweep face check cap.
        self._face_rr = 0
        self._throttled = False
        # user id -> monotonic time of their last fallback screenshot.
        self._last_polaroid: dict = {}
        self._last_advance = 0.0

    @classmethod
    def _clamp(cls, seconds) -> int:
        return max(cls.MIN_INTERVAL_SECONDS, int(seconds or 300))

    def set_interval(self, seconds: int) -> int:
        """Change the cadence of a loop that is already running.

        Re-arms the sleep against the new interval WITHOUT running an extra
        sweep: the wake used to trigger an immediate observation, which made
        the cadence look broken right after anyone changed it.
        """
        self.interval_seconds = self._clamp(seconds)
        self._rearm_only = True
        self._wake.set()
        return self.interval_seconds

    def trigger_now(self) -> None:
        """Ask the loop to sweep immediately rather than finish its sleep."""
        self._wake.set()

    @classmethod
    def memory_fraction(cls) -> float:
        """How full the container's memory cgroup is, 0.0 when unknowable."""
        def read_first(paths):
            for p in paths:
                try:
                    with open(p) as f:
                        return f.read().strip()
                except OSError:
                    continue
            return None
        cur, mx = read_first(cls.MEM_CURRENT_PATHS), read_first(cls.MEM_MAX_PATHS)
        if not cur or not mx or mx == "max":
            return 0.0
        try:
            cur_b, max_b = int(cur), int(mx)
        except ValueError:
            return 0.0
        # cgroup v1 reports "no limit" as an enormous number.
        if max_b <= 0 or max_b > (1 << 50):
            return 0.0
        return cur_b / max_b

    async def _pressure_escalate(self) -> None:
        """Throttle and, at the edge, shrink. Called by the watchdog and at
        every sweep; recovery lives in the sweep so it hysteresis-gates."""
        frac = self.memory_fraction()
        if frac >= self.MEM_SOFT_LIMIT and not self._throttled:
            self._throttled = True
            logger.warning(
                "[CAPTURE] memory at %d%% of the container limit; pausing face "
                "captures, attendance continues", int(frac * 100))
        if frac >= self.MEM_HARD_LIMIT:
            shrink = getattr(self.client, "shrink_viewport", None)
            if shrink is not None:
                try:
                    await shrink()
                except Exception as e:
                    logger.warning("emergency viewport shrink failed: %s", e)

    async def _memory_watchdog(self) -> None:
        """Check pressure every couple of seconds, not once per sweep.

        The worst spike is at join: landing in a room full of cameras spins
        up every decoder on the first gallery page at once, and a container
        died on it before the first observation finished. A per-sweep check
        structurally cannot catch that; this can.
        """
        while not self._stop.is_set():
            try:
                await self._pressure_escalate()
            except Exception as e:
                logger.debug("watchdog check failed: %s", e)
            await asyncio.sleep(self.WATCHDOG_SECONDS)

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

        # The in-page watcher checks every rendered seat about once a second.
        # When it has a fresh reading for someone, that reading IS their face
        # result this sweep, and no screenshot is taken for them: full
        # coverage at watcher speed. Anyone it cannot see (tiles unreadable,
        # watcher off, other gallery page) falls back to the screenshot
        # rotation below, so the worst case is exactly the old behaviour.
        wusers = {}
        try:
            wstate = await self.client.watcher_state()
            if wstate and wstate.get("running"):
                wusers = wstate.get("users") or {}
        except Exception:
            wusers = {}
        now_ms = time.time() * 1000

        def watcher_face(uid) -> Optional[bool]:
            s = wusers.get(str(uid))
            if not s or not s.get("readable"):
                return None
            if now_ms - (s.get("lastCheckedAt") or 0) > self.WATCHER_FRESH_MS:
                return None
            return bool(s.get("facePresent"))

        # Who is eligible for a face check this sweep. With the cap, a room
        # of thirty cameras costs the same per sweep as a room of four; the
        # cursor walks the list so nobody is starved across sweeps.
        starved = False
        eligible = []
        fallback_needed = 0
        for row in snapshot.rows:
            if self._is_self(row.user_id, row.name, ctx):
                continue
            p = participants.get(row.user_id)
            if p is not None and p.is_hold:
                continue
            if (bool(p.video_on) if p else bool(row.video_on)):
                # Covered by the watcher means no screenshot turn needed,
                # and nobody gets a fallback screenshot more often than
                # POLAROID_MIN_GAP_SECONDS regardless of the notebook pace.
                uid = str(row.user_id)
                if watcher_face(uid) is None:
                    fallback_needed += 1
                    if (time.monotonic() - self._last_polaroid.get(uid, 0.0)
                            >= self.POLAROID_MIN_GAP_SECONDS):
                        eligible.append(uid)
        if len(eligible) <= self.FACE_CHECKS_PER_SWEEP:
            face_turn = set(eligible)
        else:
            start = self._face_rr % len(eligible)
            face_turn = {eligible[(start + i) % len(eligible)]
                         for i in range(self.FACE_CHECKS_PER_SWEEP)}
            self._face_rr = (start + self.FACE_CHECKS_PER_SWEEP) % len(eligible)

        # The valve: under memory pressure, faces wait and attendance
        # continues. Unchecked people are recorded as not checked, the
        # same honesty rule as a tile that never rendered. Escalation also
        # runs from the watchdog between sweeps; recovery only here, with
        # hysteresis, so it cannot flap.
        await self._pressure_escalate()
        if self._throttled and self.memory_fraction() < self.MEM_RESUME_BELOW:
            self._throttled = False
            logger.info("[CAPTURE] memory back under %d%%; face captures resume",
                        int(self.MEM_RESUME_BELOW * 100))
        if self._throttled:
            face_turn = set()

        for row in snapshot.rows:
            if self._is_self(row.user_id, row.name, ctx):
                continue

            p = participants.get(row.user_id)
            # Waiting room, not the meeting. Recording them as present would
            # mark attendance for someone the host has not admitted, and the
            # message machinery must never reach into the waiting room.
            if p is not None and p.is_hold:
                continue
            video_on = bool(p.video_on) if p else bool(row.video_on)
            # Role has to be read live rather than cached at join, because
            # co-host is granted mid-meeting. The control plane needs it to
            # leave whoever is running the class alone.
            is_host = bool(p.is_host) if p else False
            is_cohost = bool(p.is_co_host) if p else False

            wface = watcher_face(row.user_id) if video_on else None
            data: Optional[bytes] = None
            if video_on and wface is None and str(row.user_id) in face_turn:
                self._last_polaroid[str(row.user_id)] = time.monotonic()
                try:
                    data = await self.client.capture_user(row.user_id)
                except Exception as e:
                    logger.warning("capture_user(%s) failed: %s", row.user_id, e)
                    data = None
                if data is None:
                    # Their turn came up but no tile was rendered: they are
                    # on another gallery page. Page after this sweep.
                    starved = True

            # None means "never looked", which is not the same claim as False,
            # "looked and saw nobody". Sending False for an unexamined frame
            # made every tick read downstream as a completed face check, so a
            # report could show 0 of 27 checks failing when no check ever ran.
            # Per-user frames are unavailable on this SDK, so today this is
            # always None; it must stay honest if that ever changes.
            face: Optional[bool] = wface
            if wface is None and data:
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
                "face_checked": (data is not None) or (wface is not None),
                "is_host": is_host,
                "is_cohost": is_cohost,
            }
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
            # The manifest exists to account for captured pixels. At a one
            # second pace with the watcher carrying faces, most ticks have
            # no pixels, and posting an empty manifest per person per
            # second would double the wire traffic for nothing. The
            # attendance row above carries presence, camera, and face.
            if data is not None or stored:
                await self.backend.post_screenshot(manifest)
            rows.append(attendance)

        # The whole sweep's attendance goes in one request. Per-person posts
        # made the wire the bottleneck: 25 people at a one second pace was
        # 25 sequential round trips per sweep.
        if rows:
            await self.backend.post_attendance_batch(
                session_ref=ctx.session_ref, runtime_id=ctx.runtime_id,
                captured_at=captured_at, rows=rows)

        # The other half of the "handful at a time" design: the browser only
        # decodes the gallery page it shows, so when someone's face turn
        # found no rendered tile, or there are more cameras than one page
        # holds, step the gallery so the next page gets its turn. One step
        # per sweep; the capture rotation and this rotation mesh over a few
        # sweeps to cover everyone at a constant memory cost.
        # Flip the gallery when people still need coverage the current page
        # cannot give them. Not while throttled (fresh pages spin up fresh
        # decoders, the opposite of what memory pressure needs) and never
        # faster than the dwell gap, or a fast notebook pace would flip to
        # tiles still loading and photograph nothing forever.
        if ((starved or fallback_needed > self.FACE_CHECKS_PER_SWEEP)
                and not self._throttled
                and time.monotonic() - self._last_advance >= self.PAGE_FLIP_MIN_GAP_SECONDS):
            advance = getattr(self.client, "gallery_advance", None)
            if advance is not None:
                self._last_advance = time.monotonic()
                try:
                    await advance()
                except Exception as e:
                    logger.debug("gallery advance skipped: %s", e)

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
        watchdog = asyncio.create_task(self._memory_watchdog())
        try:
            while not self._stop.is_set():
                elapsed = 0.0
                if self._rearm_only:
                    # The wake was an interval change, not a request to observe.
                    self._rearm_only = False
                else:
                    started = time.monotonic()
                    try:
                        await self.run_once(ctx)
                    except Exception as e:  # keep the loop alive across transient errors
                        logger.warning("[CAPTURE] run_once error: %s", e)
                    elapsed = time.monotonic() - started
                await self._sleep_until_next(elapsed)
        finally:
            watchdog.cancel()
        logger.info("[CAPTURE] attendance loop stopped for session %s", ctx.session_ref)

    async def _sleep_until_next(self, elapsed: float = 0.0) -> None:
        """Wait out the rest of the interval, but wake early on stop or a nudge.

        The interval is measured start to start, so the sweep's own duration
        counts against it: "observe every 10 seconds" used to mean "rest 10
        seconds after each sweep", and with 4 to 8 second sweeps the real
        cadence sat at 14 to 18. A sweep that outruns the interval gets a one
        second breather so the API stays responsive, and that is the floor.
        """
        timeout = max(1.0, self.interval_seconds - elapsed)
        waiters = [asyncio.ensure_future(self._stop.wait()),
                   asyncio.ensure_future(self._wake.wait())]
        try:
            await asyncio.wait(waiters, timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            self._wake.clear()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()          # do not sit out the interval before exiting
