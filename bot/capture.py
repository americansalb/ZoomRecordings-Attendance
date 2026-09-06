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
    # The last valve: above this, on two readings in a row, the bot
    # leaves the meeting on its own. Measured 2026-09-06: a room of 23
    # climbs about 8 MB a minute whatever the bot renders, and a browser
    # allowed to reach the ceiling freezes the whole machine for an hour
    # (health page dead, no replacement possible). A bot that leaves at
    # 95 percent keeps the machine answering, and the control plane sends
    # a replacement within a few minutes: two minutes of gap against an
    # hour of nothing. A bot failure is never an attendance failure.
    MEM_LEAVE_LIMIT = 0.95
    MEM_LEAVE_STRIKES = 2
    WATCHDOG_SECONDS = 2

    # A watcher reading older than this is not evidence about right now.
    WATCHER_FRESH_MS = 5000

    # The page now loads the detector at join, the cheapest moment the
    # container ever sees, so arming here only STARTS the watch loop and
    # costs almost nothing. The 0.72 gate came from when arming carried
    # the detector load itself, a bite of roughly 15 percent of a 512 MB
    # box; measured mid-meeting with a gallery decoding, memory routinely
    # sat above it and the watcher never armed, which is why entire
    # sessions ended with zero face checks. The gate now matches the
    # throttle line: face work starts whenever face work is allowed at all.
    WATCHER_ARM_MAX_MEM = MEM_SOFT_LIMIT
    MEM_CURRENT_PATHS = ("/sys/fs/cgroup/memory.current",
                         "/sys/fs/cgroup/memory/memory.usage_in_bytes")
    MEM_MAX_PATHS = ("/sys/fs/cgroup/memory.max",
                     "/sys/fs/cgroup/memory/memory.limit_in_bytes")
    # memory.current counts file pages the machine merely read from disk
    # (Chromium's own binary, the vendored SDK) alongside what it holds.
    # The kernel gives those back before it kills anything, so the meter
    # subtracts the inactive file cache, the way docker stats and the
    # Kubernetes working set do. Measured: a fresh container read 93
    # percent while launching a browser that later rested at 69.
    MEM_STAT_PATHS = ("/sys/fs/cgroup/memory.stat",
                      "/sys/fs/cgroup/memory/memory.stat")
    MEM_STAT_INACTIVE_KEYS = ("inactive_file", "total_inactive_file")

    def __init__(
        self,
        client: MeetingClient,
        backend,
        storage,
        *,
        interval_seconds: int,
        store_images: bool,
        room_snapshot_seconds: int = 0,
        student_photo_seconds: int = 0,
        lookout: bool = False,
        face_detector: Callable[[bytes], bool] = default_face_detector,
    ):
        self.client = client
        self.backend = backend
        self.storage = storage
        self.interval_seconds = self._clamp(interval_seconds)
        # Lookout: presence, camera state, and chat only. Every pixel path
        # (face checks, the watcher, screenshots, room shots, gallery
        # paging) stays off for the life of the session. Video decode is
        # what fills a small container; the roster costs almost nothing,
        # so a lookout survives a room of any size on the same machine.
        self.lookout = bool(lookout)
        self.store_images = store_images and not self.lookout
        # A picture of the WHOLE gallery view on its own relaxed clock,
        # saved to storage for later review. 0 means off, and off is the
        # default: a session has to ask for room evidence. A lookout shows
        # four thumbnails, so a room shot of it would be evidence of
        # nothing: forced off.
        self.room_snapshot_seconds = (
            0 if self.lookout else max(0, int(room_snapshot_seconds or 0)))
        # None means no shot yet: the first one goes immediately, so the
        # evidence starts when the bot does instead of one window late.
        self._last_room_shot = None
        # One student at a time, each on their own clock: every
        # `student_photo_seconds` the person longest overdue gets one
        # picture, filed in a folder of their own. 0 means off. A room
        # picture only proves the class looked normal; a per-person photo
        # is the one that can answer whether a named student was there
        # and on camera at a given minute. Off for a lookout, which never
        # renders a tile to photograph.
        self.student_photo_seconds = (
            0 if self.lookout else max(0, int(student_photo_seconds or 0)))
        # user id -> monotonic time of their last student photo.
        self._last_student_photo: dict = {}
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
        # The bot's own camera picture is cosmetic, so at the memory hard
        # limit it is the first thing switched off, once.
        self._camera_stopped = False
        self._leave_strikes = 0
        self._leaving = False
        # user id -> monotonic time of their last fallback screenshot.
        self._last_polaroid: dict = {}
        self._last_advance = 0.0
        # Grid proctoring: walk every gallery page so nobody goes longer
        # than the coverage window (room_snapshot_seconds) unseen, at a
        # one-page memory cost. These carry the live state for logs.
        self._grid_pages = 1
        self._grid_cover_seconds = 0.0
        self._grid_last_ok = None

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
    def _read_first(cls, paths):
        for p in paths:
            try:
                with open(p) as f:
                    return f.read().strip()
            except OSError:
                continue
        return None

    @classmethod
    def _inactive_file_bytes(cls) -> int:
        """The reclaimable file cache the cgroup is holding, 0 when unknown."""
        text = cls._read_first(cls.MEM_STAT_PATHS)
        if not text:
            return 0
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in cls.MEM_STAT_INACTIVE_KEYS:
                try:
                    return max(0, int(parts[1]))
                except ValueError:
                    return 0
        return 0

    @classmethod
    def memory_fraction(cls, *, with_cache: bool = False) -> float:
        """How full the container's memory cgroup is, 0.0 when unknowable.

        The working set by default: what is held, minus the inactive file
        cache the kernel reclaims before it kills anything. with_cache
        gives the raw cgroup number for comparison on the health page.
        """
        cur, mx = cls._read_first(cls.MEM_CURRENT_PATHS), cls._read_first(cls.MEM_MAX_PATHS)
        if not cur or not mx or mx == "max":
            return 0.0
        try:
            cur_b, max_b = int(cur), int(mx)
        except ValueError:
            return 0.0
        # cgroup v1 reports "no limit" as an enormous number.
        if max_b <= 0 or max_b > (1 << 50):
            return 0.0
        if not with_cache:
            cur_b = max(0, cur_b - cls._inactive_file_bytes())
        return cur_b / max_b

    @classmethod
    def memory_breakdown(cls):
        """Where the memory actually goes, per process, so a fix targets
        the real consumer instead of a guess. Reads each process's PSS
        (proportional set size, shared pages fairly split) from
        /proc/<pid>/smaps_rollup and groups by program name. Returns a
        list of {name, pss_mb, procs} biggest first, plus the total. Best
        effort: a kernel without smaps_rollup, or a process that exits
        mid-read, is skipped rather than fatal."""
        import glob
        groups = {}
        for path in glob.glob("/proc/[0-9]*"):
            pid = path.rsplit("/", 1)[1]
            try:
                with open(f"/proc/{pid}/comm") as f:
                    name = f.read().strip() or "?"
                pss_kb = 0
                with open(f"/proc/{pid}/smaps_rollup") as f:
                    for line in f:
                        if line.startswith("Pss:"):
                            pss_kb += int(line.split()[1])
                            break
            except (OSError, ValueError):
                continue
            if pss_kb <= 0:
                continue
            g = groups.setdefault(name, [0, 0])
            g[0] += pss_kb
            g[1] += 1
        rows = sorted(
            ({"name": n, "pss_mb": round(v[0] / 1024, 1), "procs": v[1]}
             for n, v in groups.items()),
            key=lambda r: r["pss_mb"], reverse=True)
        total = round(sum(r["pss_mb"] for r in rows), 1)
        return {"total_pss_mb": total, "by_process": rows[:12]}

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
            # The cat yields before the class does: the bot's camera picture
            # costs an encoder, and an encoder is the first thing to give
            # back when the machine is this close to the line.
            stop = getattr(self.client, "stop_video", None)
            if stop is not None and not self._camera_stopped:
                self._camera_stopped = True
                try:
                    await stop()
                    logger.warning(
                        "[CAPTURE] memory at %d%%; the bot's camera picture is switched "
                        "off to make room", int(frac * 100))
                except Exception as e:
                    logger.warning("camera picture stop failed: %s", e)
        # The last valve: leave before the machine freezes.
        if frac >= self.MEM_LEAVE_LIMIT:
            self._leave_strikes += 1
            if self._leave_strikes >= self.MEM_LEAVE_STRIKES and not self._leaving:
                self._leaving = True
                detail = (f"left the meeting to keep its computer from freezing "
                          f"(memory at {int(frac * 100)} percent); a replacement follows")
                logger.error("[CAPTURE] %s", detail)
                asyncio.create_task(self._leave_for_memory(detail))
        else:
            self._leave_strikes = 0

    async def _leave_for_memory(self, detail: str) -> None:
        """Say goodbye the way a meeting ending does, so the control plane
        treats it as an interruption and sends a replacement, then go."""
        try:
            handler = getattr(self.client, "on_lifecycle", None)
            if handler is not None:
                await handler("left", detail)
        except Exception as e:
            logger.warning("[CAPTURE] leave notice failed: %s", e)
        self.stop()
        try:
            await self.client.leave()
        except Exception as e:
            logger.warning("[CAPTURE] leave failed: %s", e)

    @staticmethod
    def _grid_interval(window_seconds, pages, min_flip):
        """Seconds between grid shots so all `pages` pages are photographed
        within `window_seconds`. One page is shot per tick and the gallery
        advances one page per tick, so P ticks cover everyone; the tick is
        window/pages, floored at min_flip because a page cannot be flipped
        and painted faster than that. With one page there is nothing to
        walk, so a shot every window seconds is the whole job."""
        pages = max(1, int(pages))
        if pages <= 1:
            return max(1.0, float(window_seconds))
        return max(float(window_seconds) / pages, float(min_flip))

    async def _grid_sleep(self, seconds) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.5, float(seconds)))
        except asyncio.TimeoutError:
            pass

    async def _grid_loop(self, ctx) -> None:
        """Photograph the whole class as a grid, walking every gallery page
        so nobody goes longer than the coverage window unseen, at a
        one-page memory cost. Holds under memory pressure and never lets a
        failed shot crash the process."""
        folder = f"LiveTutor {ctx.session_label}".strip()
        # The first shot goes immediately, so evidence starts with the bot.
        while not self._stop.is_set():
            window = max(1, int(self.room_snapshot_seconds or 0))
            if window <= 0:
                await self._grid_sleep(5)
                continue
            # A fresh page spins up fresh decoders, the opposite of what a
            # tight machine needs. Hold, and let coverage resume when the
            # pressure lifts; the record is never a reason to push the box
            # over its line.
            if self._throttled:
                self._grid_cover_seconds = 0.0
                await self._grid_sleep(min(window, 5))
                continue
            pages = 1
            try:
                info = await self.client.gallery_info() or {}
                pages = max(1, int(info.get("pages") or 1))
            except Exception as e:
                logger.debug("gallery info unavailable: %s", e)
            self._grid_pages = pages
            try:
                shot = await self.client.page_screenshot()
                if shot:
                    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
                    await self.storage.upload(
                        data=shot,
                        filename=f"room_{_safe(ctx.session_label)}_{ts}.png",
                        session_folder=folder)
                    self._grid_last_ok = time.time()
                    self._last_room_shot = time.monotonic()
            except Exception as e:
                logger.warning("grid room shot failed: %s", e)
            interval = self._grid_interval(window, pages, self.PAGE_FLIP_MIN_GAP_SECONDS)
            # The honest coverage window: what we actually achieve, which is
            # the target when the machine can flip fast enough, and longer
            # when a big class needs more pages than the flip gap allows.
            self._grid_cover_seconds = round(interval * pages, 1)
            if pages > 1:
                try:
                    await self.client.gallery_advance()
                    self._last_advance = time.monotonic()
                except Exception as e:
                    logger.debug("grid advance skipped: %s", e)
            await self._grid_sleep(interval)

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
            wstate = None if self.lookout else await self.client.watcher_state()
            if wstate and wstate.get("running"):
                wusers = wstate.get("users") or {}
            elif wstate is not None and not self._throttled:
                mem = self.memory_fraction()
                if mem < self.WATCHER_ARM_MAX_MEM:
                    # Cameras may be on while the detector is not loaded
                    # yet. Arm only with real headroom, measured now.
                    arm = getattr(self.client, "watcher_arm", None)
                    if arm is not None:
                        armed = await arm()
                        if armed and armed.get("ok"):
                            logger.info("[CAPTURE] seat watcher armed (memory at %d%%)",
                                        int(mem * 100))
                elif time.monotonic() - getattr(self, "_arm_declined_at", 0.0) > 60:
                    # Declining silently hid an entire failure for a whole
                    # session. Say it, at most once a minute.
                    self._arm_declined_at = time.monotonic()
                    logger.info(
                        "[CAPTURE] watcher not armed: memory at %d%%, needs under %d%%; "
                        "faces ride the screenshot fallback",
                        int(mem * 100), int(self.WATCHER_ARM_MAX_MEM * 100))
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
        # A lookout takes no screenshot turns at all: presence and camera
        # state for the whole room already came from the one roster read.
        for row in ([] if self.lookout else snapshot.rows):
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
        # At a fast observation pace the sweep itself must stay near the
        # interval, and screenshots are the one step that cannot. Four
        # captures at up to four seconds each inside a one second notebook
        # meant the loop ran flat out doing screenshots back to back, CPU
        # pinned, for the whole meeting: the cadence lied and the container
        # lived on the edge. One capture per fast sweep still rotates
        # through everyone; the watcher carries the rest.
        cap = self.FACE_CHECKS_PER_SWEEP if self.interval_seconds >= 10 else 1
        if len(eligible) <= cap:
            face_turn = set(eligible)
        else:
            start = self._face_rr % len(eligible)
            face_turn = {eligible[(start + i) % len(eligible)]
                         for i in range(cap)}
            self._face_rr = (start + cap) % len(eligible)

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
        if ((starved or fallback_needed > cap)
                and self.room_snapshot_seconds == 0
                and not self._throttled
                and time.monotonic() - self._last_advance >= self.PAGE_FLIP_MIN_GAP_SECONDS):
            advance = getattr(self.client, "gallery_advance", None)
            if advance is not None:
                self._last_advance = time.monotonic()
                try:
                    await advance()
                except Exception as e:
                    logger.debug("gallery advance skipped: %s", e)

        # The whole-room grid shot used to ride this sweep. It now runs on
        # its own loop (_grid_loop) so it can walk every gallery page fast
        # enough that everyone is photographed within the coverage window,
        # instead of at most once per observation interval.

        # One student, photographed on their own clock and filed in a
        # folder of their own. Whoever is longest overdue goes next, so a
        # class rotates fairly instead of favouring whoever sits on the
        # first gallery page. Only people with a camera on are eligible:
        # a camera-off student has no tile, so there is nothing to
        # photograph and a missing photo must never be mistaken for a
        # photo of an empty seat. One capture per sweep, skipped under
        # memory pressure like every other pixel path.
        if self.student_photo_seconds > 0 and not self._throttled:
            now_mono = time.monotonic()
            due = []
            for row in snapshot.rows:
                if self._is_self(row.user_id, row.name, ctx):
                    continue
                p = participants.get(row.user_id)
                if p is not None and (p.is_hold or p.is_host):
                    continue
                if not (bool(p.video_on) if p else bool(row.video_on)):
                    continue
                last = self._last_student_photo.get(str(row.user_id))
                if last is not None and now_mono - last < self.student_photo_seconds:
                    continue
                # Never photographed yet sorts first, then longest overdue.
                due.append((last if last is not None else -1.0, row))
            if due:
                due.sort(key=lambda x: x[0])
                row = due[0][1]
                self._last_student_photo[str(row.user_id)] = now_mono
                try:
                    shot = await self.client.capture_user(row.user_id)
                    if shot:
                        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
                        # The folder is named for the person and carries
                        # their Zoom id, so two students with the same
                        # display name never share a folder.
                        await self.storage.upload(
                            data=shot,
                            filename=f"{_safe(row.name)}_{ts}.png",
                            session_folder=folder,
                            subfolder=f"{_safe(row.name)}_{_safe(str(row.user_id))}")
                except Exception as e:
                    logger.warning("student photo failed for %s: %s", row.user_id, e)

        return rows

    async def run(self, ctx: CaptureContext) -> None:
        logger.info("[CAPTURE] attendance loop started for session %s every %ss%s",
                    ctx.session_ref, self.interval_seconds,
                    " (lookout: no video work)" if self.lookout else "")
        try:
            if not await self.client.capture_supported():
                logger.info(
                    "[CAPTURE] per-user frame capture unavailable on this SDK; "
                    "recording presence and camera state only")
        except Exception:
            pass
        watchdog = asyncio.create_task(self._memory_watchdog())
        grid = None
        if self.room_snapshot_seconds > 0 and not self.lookout:
            grid = asyncio.create_task(self._grid_loop(ctx))
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
            if grid is not None:
                grid.cancel()
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
