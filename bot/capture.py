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
import math
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

    # The shortest turn a gallery page gets. A freshly shown tile needs a
    # second or two before it displays real video, and the watcher checks
    # each seat about once a second, so ten seconds is several good
    # readings per person per turn. Turning faster would photograph
    # loading tiles and churn Zoom's video subscriptions.
    PAGE_FLIP_MIN_GAP_SECONDS = 10

    # Screenshot checks for people on screen whose tile the watcher could
    # not read, per page turn. Each one screenshots a tile and runs the
    # detector, the heavy step; presence and camera state for the whole
    # room come from one roster read. Whoever has gone longest without a
    # face result goes first, so nobody on a page is starved.
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

    # A face reading counts once, at the first observation after it was
    # made (see _reading). The very first observation of a session has no
    # earlier one to measure from, so it takes readings this recent.
    FIRST_READ_WINDOW_SECONDS = 5

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
        # Gallery page number -> monotonic time of its last room picture.
        # Each page is photographed in its own turn, so a room picture
        # covers the whole class rather than the first page over and over.
        self._room_shot_at: dict = {}
        # One student at a time, each on their own clock: every
        # `student_photo_seconds` the person longest overdue gets one
        # picture, filed in a folder of their own. 0 means off. A room
        # picture only proves the class looked normal; a per-person photo
        # is the one that can answer whether a named student was there
        # and on camera at a given minute. Off for a lookout, which never
        # renders a tile to photograph.
        self.student_photo_seconds = (
            0 if self.lookout else max(0, int(student_photo_seconds or 0)))
        # user id -> monotonic time of their last student photo that was
        # actually taken. A try that found no tile is not their turn.
        self._last_student_photo: dict = {}
        # A photo falls due once per observation, and goes to whoever in
        # the class is longest overdue, taken when their page comes up.
        self._photo_owed_since: Optional[float] = None
        # user id -> monotonic time before which a failed photo is not retried.
        self._photo_retry_at: dict = {}
        self.face_detector = face_detector
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        # Serialises the scheduled sweep against an operator-triggered one, so
        # "take attendance now" cannot interleave with a tick already running
        # and report a half-built ledger.
        self._sweep_lock = asyncio.Lock()
        self._self_id: Optional[str] = None
        self._rearm_only = False
        self._throttled = False
        # The bot's own camera picture is cosmetic, so at the memory hard
        # limit it is the first thing switched off, once.
        self._camera_stopped = False
        self._leave_strikes = 0
        self._leaving = False
        # user id -> monotonic time of their last fallback screenshot.
        self._last_polaroid: dict = {}
        # user id -> (wall ms, face found): a screenshot check made in a
        # page's turn, counted at the first observation after it.
        self._fallback: dict = {}
        # user id -> the screenshot record for their newest check, sent with
        # the next observation (see _sweep): the backend files a record as a
        # sample of that person at its own time, so one sent at the moment of
        # the check would be a second sample between two observations.
        self._pending_shots: dict = {}
        # user id -> wall ms of the newest reading already counted, so no
        # reading is counted twice.
        self._counted_ms: dict = {}
        # user id -> wall ms of their last face result of any kind. The
        # screenshot fallback serves whoever has waited longest first.
        self._last_face_ms: dict = {}
        # Wall ms of the previous observation. A face reading counts once,
        # at the first observation after it was made.
        self._prev_read_ms: Optional[float] = None
        # The gallery walk (see _between_sweeps). One page is on screen at
        # a time; these say which, when it came up, who with a camera on
        # has had their turn in this trip round the gallery, and how the
        # last full trip went.
        self._paged: Optional[bool] = None       # unknown until the first turn answers
        self._page_index = 0
        # The first page was up before the loop began, so the first turn
        # takes the watcher's readings as recent as the first read does.
        self._page_up_ms = (time.time() - self.FIRST_READ_WINDOW_SECONDS) * 1000
        self._last_turn_mono = 0.0
        self._trip_seen: set = set()
        self._trip_pages = 1
        self._trip_started = time.monotonic()
        self._last_trip: Optional[dict] = None
        self._turns_ok = 0
        self._turn_failures = 0
        self._last_turn: Optional[dict] = None
        self._turn_fail_logged_at = 0.0

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
    def memory_limit_mb(cls) -> int:
        """How much memory this container is allowed, in MB; 0 when the
        cgroup does not say. What the box costs is read from the box, so
        a bigger instance needs no setting changed anywhere."""
        mx = cls._read_first(cls.MEM_MAX_PATHS)
        if not mx or mx == "max":
            return 0
        try:
            max_b = int(mx)
        except ValueError:
            return 0
        if max_b <= 0 or max_b > (1 << 50):
            return 0
        return max_b // (1024 * 1024)

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

    # ── The gallery walk ────────────────────────────────────────────────
    #
    # Only a tile on screen can have its face checked, and the gallery
    # shows one page at a time: four tiles at the bot's size, one of them
    # the bot. So the walk decides whether every student gets checked, and
    # it is built to be even by construction:
    #
    #   - Pages turn in step with the observations: once inside each
    #     observation, after that page's pixel work and before the read,
    #     then as many more times before the next observation as turns of
    #     PAGE_FLIP_MIN_GAP_SECONDS fit, never more than the gallery has
    #     pages with cameras on them. Below that pace, once every so many
    #     observations.
    #   - A face reading counts once, at the first observation after it
    #     was made (_reading), so one turn of a page gives each person on
    #     it one reading, whichever part of the interval it fell in.
    #   - A trip round the gallery ends as soon as everyone with a camera
    #     on has been on screen in it, and the next starts at page one, so
    #     no page is skipped and none gets two turns in one trip.
    #
    # Every page comes round equally often, and so does every face.
    # FOUND 2026-09-25: before this the gallery never turned at all (the
    # page buttons were looked for under the wrong words), so the same
    # three students got every check and nobody else got one.

    async def _rest_until(self, deadline: float) -> bool:
        """Wait until `deadline` (monotonic), or less when stopped or woken.

        True means something woke the loop (a stop, "take attendance now",
        or a new pace) and the caller should return at once."""
        if self._stop.is_set():
            return True
        timeout = deadline - time.monotonic()
        if timeout > 0 and not self._wake.is_set():
            waiters = [asyncio.ensure_future(self._stop.wait()),
                       asyncio.ensure_future(self._wake.wait())]
            try:
                await asyncio.wait(waiters, timeout=timeout,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
        woke = self._stop.is_set() or self._wake.is_set()
        self._wake.clear()
        return woke

    def _turn_due_at_read(self) -> bool:
        """Whether this observation turns the page, between its pixel work
        and its read. Turning there, rather than after the read, means a
        reading can never straddle two observations: the page read is the
        page that just had its turn, and the new page has shown nothing yet."""
        if self.lookout or self._throttled:
            return False
        since = time.monotonic() - self._last_turn_mono
        if self._paged is False:
            # One page, nothing to turn. Look again once a minute in case
            # the class has grown past a page.
            return since >= 60
        if self.interval_seconds >= self.PAGE_FLIP_MIN_GAP_SECONDS:
            return True
        return since >= self.PAGE_FLIP_MIN_GAP_SECONDS

    async def _between_sweeps(self, ctx: CaptureContext, started: float) -> None:
        """The time between two observations, with the gallery turned on the
        way. The interval is measured start to start, so the sweep's own
        time counts against it: "observe every 10 seconds" used to mean "rest
        10 seconds after each sweep", and with 4 to 8 second sweeps the real
        cadence sat at 14 to 18. A sweep that outruns the interval gets a one
        second breather so the API stays responsive, and that is the floor."""
        deadline = max(started + float(self.interval_seconds), time.monotonic() + 1.0)
        if self.lookout:
            await self._rest_until(deadline)
            return
        floor = float(self.PAGE_FLIP_MIN_GAP_SECONDS)
        remaining = deadline - time.monotonic()
        k = max(1, int(remaining // floor))
        if self._paged:
            # The observation already turned once. No more turns in an
            # interval than the gallery has pages worth showing.
            if self._last_trip and self._last_trip.get("pages"):
                k = min(k, max(1, int(self._last_trip["pages"])))
        elif self.room_snapshot_seconds > 0:
            # One page: nothing to turn. Extra looks inside the interval are
            # needed only for room pictures due more often than it.
            k = max(1, min(k, math.ceil(remaining / self.room_snapshot_seconds)))
        else:
            k = 1
        slot = remaining / k
        t0 = time.monotonic()
        for j in range(1, k):
            if await self._rest_until(t0 + j * slot):
                return
            # This page's turn is over: its pixel work, then the next page.
            # Under the sweep's lock, so a "take attendance now" never reads
            # the room halfway through a turn.
            async with self._sweep_lock:
                await self._visit_pass(ctx)
                if self._paged and not self._throttled:
                    await self._turn(ctx)
        await self._rest_until(deadline)

    async def _cameras_on(self, ctx: CaptureContext) -> set:
        try:
            parts = await self.client.list_participants()
        except Exception:
            return set()
        return {str(p.user_id) for p in parts
                if p.video_on and not p.is_hold
                and not self._is_self(p.user_id, p.name, ctx)}

    async def _turn(self, ctx: CaptureContext) -> None:
        """Turn the gallery: back to page one when everyone with a camera
        on has had their turn in this trip, otherwise one page on (Zoom's
        last page goes back to the first). What happened is kept, and a turn
        that failed is said in the log, at most once a minute."""
        cams = await self._cameras_on(ctx)
        direction = "first" if cams and cams <= self._trip_seen else "next"
        turn = getattr(self.client, "gallery_turn", None)
        try:
            res = (await turn(direction)) if turn is not None else {"ok": True, "moved": "none"}
        except Exception as e:
            res = {"ok": False, "why": f"the turn failed: {str(e)[:120]}"}
        res = res or {}
        now = time.monotonic()
        self._last_turn_mono = now
        self._page_up_ms = time.time() * 1000
        moved = res.get("moved")
        self._last_turn = {"at": round(time.time(), 1), "asked": direction,
                           "ok": bool(res.get("ok")), "moved": moved,
                           "why": res.get("why")}
        if not res.get("ok"):
            self._turn_failures += 1
            if now - self._turn_fail_logged_at > 60:
                self._turn_fail_logged_at = now
                buttons = res.get("buttons")
                logger.warning(
                    "[CAPTURE] could not turn the gallery page: %s%s",
                    res.get("why") or "no reason given",
                    f"; buttons on the page: {buttons}" if buttons else "")
            return
        if moved == "next":
            self._paged = True
            self._turns_ok += 1
            # The page says where it is when it can; Zoom moves the gallery
            # by itself when people leave, so its word beats a count kept here.
            page = res.get("page")
            self._page_index = page if isinstance(page, int) and page > 0 else self._page_index + 1
            self._trip_pages += 1
            return
        if moved == "first":
            self._paged = True
            self._turns_ok += 1
        else:
            self._paged = False
        # Back at page one, or a gallery with one page: the trip is over.
        self._last_trip = {
            "seconds": round(now - self._trip_started, 1),
            "pages": self._trip_pages,
            "cameras": len(cams),
            "seen": len(cams & self._trip_seen),
        }
        self._page_index = 0
        self._trip_pages = 1
        self._trip_seen = set()
        self._trip_started = now

    async def _rendered(self) -> Optional[set]:
        """User ids with a video tile on screen now, or None when that
        cannot be read (which counts nobody as seen, and checks everyone)."""
        fn = getattr(self.client, "rendered_users", None)
        if fn is None:
            return None
        try:
            ids = await fn()
        except Exception as e:
            logger.debug("rendered tiles unavailable: %s", e)
            return None
        return None if ids is None else {str(i) for i in ids}

    async def _watcher_users(self, *, arm: bool) -> dict:
        """The seat watcher's per-person readings, {} when it has none.

        The in-page watcher checks every seat on screen about once a
        second; its readings are the face results, with a screenshot check
        only for someone it could not read. With arm, a watcher that is
        loaded but not yet watching is started when memory allows, and a
        refusal is said, at most once a minute."""
        if self.lookout:
            return {}
        try:
            wstate = await self.client.watcher_state()
            if wstate and wstate.get("running"):
                return wstate.get("users") or {}
            if arm and wstate is not None and not self._throttled:
                mem = self.memory_fraction()
                if mem < self.WATCHER_ARM_MAX_MEM:
                    # Cameras may be on while the detector is not loaded
                    # yet. Arm only with real headroom, measured now.
                    arm_fn = getattr(self.client, "watcher_arm", None)
                    if arm_fn is not None:
                        armed = await arm_fn()
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
            return {}
        return {}

    @staticmethod
    def _watcher_reading(wusers: dict, uid) -> Optional[tuple]:
        """(wall ms, face found) from the watcher's last real read of this
        person's tile, or None. A page from before readAt existed carries
        lastCheckedAt only, which counts when that check was readable."""
        s = wusers.get(str(uid)) if wusers else None
        if not s or "facePresent" not in s:
            return None
        at = s.get("readAt")
        if at is None and s.get("readable"):
            at = s.get("lastCheckedAt")
        if at is None:
            return None
        return (float(at), bool(s.get("facePresent")))

    def _reading(self, uid, wusers: dict, since_ms: float) -> Optional[tuple]:
        """The face reading this observation counts for one person: the
        newest reading, from the watcher or a screenshot check, made since
        the previous observation and not already counted. None means their
        face was not looked at in that time, which is never the same claim
        as looked at and not found."""
        best = None
        w = self._watcher_reading(wusers, uid)
        if w and w[0] >= since_ms:
            best = w
        fb = self._fallback.get(str(uid))
        if fb and fb[0] >= since_ms and (best is None or fb[0] > best[0]):
            best = fb
        if best is None or best[0] <= self._counted_ms.get(str(uid), float("-inf")):
            return None
        return best

    async def _visit_pass(self, ctx: CaptureContext) -> None:
        """The pixel work for the page on screen, at the end of its turn."""
        if self.lookout or self._throttled:
            return
        try:
            participants = {p.user_id: p for p in await self.client.list_participants()}
        except Exception as e:
            logger.debug("page turn: roster unavailable: %s", e)
            return
        await self._pass(ctx, participants, await self._watcher_users(arm=False))

    async def _pass(self, ctx: CaptureContext, participants: dict, wusers: dict) -> None:
        """Everything that needs the pixels of the page on screen, done once
        per turn of that page: a screenshot check for anyone on it the
        watcher could not read in this turn, the student photo when the
        person longest overdue is on it, and the page's room picture when
        it is due. Only people whose tile is on screen are tried: a try on
        somebody on another page finds nothing, and used to cost them their
        turn."""
        if self.lookout or self._throttled:
            return
        rendered = await self._rendered()
        on_screen = []
        for uid, p in participants.items():
            if self._is_self(uid, p.name, ctx) or p.is_hold or not p.video_on:
                continue
            if rendered is not None and str(uid) not in rendered:
                continue
            on_screen.append(p)
        if rendered is not None:
            # Who has had their turn in this trip round the gallery. Only a
            # tile Zoom actually rendered counts; not knowing counts nobody,
            # so a failed read walks the gallery on rather than back.
            self._trip_seen.update(str(p.user_id) for p in on_screen)
        since_ms = max(self._page_up_ms, self._prev_read_ms or 0.0)
        await self._fallback_checks(ctx, on_screen, wusers, since_ms)
        await self._student_photo(ctx, on_screen, participants)
        await self._room_picture(ctx)

    async def _fallback_checks(self, ctx: CaptureContext, on_screen: list,
                               wusers: dict, since_ms: float) -> None:
        now_mono = time.monotonic()
        needs = []
        for p in on_screen:
            uid = str(p.user_id)
            w = self._watcher_reading(wusers, uid)
            if w and w[0] >= since_ms:
                continue                      # the watcher read them in this turn
            fb = self._fallback.get(uid)
            if fb and fb[0] >= since_ms:
                continue                      # already checked in this turn
            if now_mono - self._last_polaroid.get(uid, float("-inf")) < self.POLAROID_MIN_GAP_SECONDS:
                continue
            needs.append(p)
        # Whoever has gone longest without any face result goes first.
        needs.sort(key=lambda p: self._last_face_ms.get(str(p.user_id), float("-inf")))
        # At a fast observation pace the loop must stay near its interval,
        # and screenshots are the one step that cannot: one per turn there.
        cap = self.FACE_CHECKS_PER_SWEEP if self.interval_seconds >= 10 else 1
        folder = f"LiveTutor {ctx.session_label}".strip()
        for p in needs[:cap]:
            uid = str(p.user_id)
            self._last_polaroid[uid] = time.monotonic()
            try:
                data = await self.client.capture_user(p.user_id)
            except Exception as e:
                logger.warning("capture_user(%s) failed: %s", p.user_id, e)
                data = None
            if not data:
                continue
            # A frame was examined: from here the answer is True or False,
            # never None. OpenCV runs off the event loop so chat and the
            # other captures are not stalled for the length of a detection.
            found = False
            try:
                found = bool(await asyncio.to_thread(self.face_detector, data))
            except Exception as e:
                logger.warning("face check failed for %s: %s", p.user_id, e)
            at_ms = time.time() * 1000
            self._fallback[uid] = (at_ms, found)
            stored = False
            image_url = None
            drive_file_id = None
            if self.store_images:
                ts = time.strftime("%Y-%m-%d_%H-%M-%S")
                filename = f"{_safe(ctx.session_label)}_{_safe(p.name)}_{ts}.png"
                drive_file_id, image_url = await self.storage.upload(
                    data=data, filename=filename, session_folder=folder)
                stored = bool(drive_file_id or image_url)
            # The manifest exists to account for captured pixels. It goes
            # with the next observation, stamped with its time.
            self._pending_shots[uid] = {
                "stored": stored,
                "image_url": image_url,
                "drive_file_id": drive_file_id,
            }

    async def _student_photo(self, ctx: CaptureContext, on_screen: list,
                             participants: dict) -> None:
        """One student photographed per observation, the one in the whole
        class longest overdue, taken when their page comes up. If they have
        not come on screen in a full trip round the gallery (their tile
        never renders), the longest overdue person on this page goes
        instead, so the photo is never held for someone the bot cannot see.
        Only people with a camera on are photographed: a camera-off student
        has no tile, and a missing photo must never be mistaken for a photo
        of an empty seat."""
        if self.student_photo_seconds <= 0 or self._photo_owed_since is None:
            return
        now = time.monotonic()

        def eligible(p) -> bool:
            if p.is_host:
                return False
            uid = str(p.user_id)
            last = self._last_student_photo.get(uid)
            if last is not None and now - last < self.student_photo_seconds:
                return False
            return now >= self._photo_retry_at.get(uid, float("-inf"))

        def overdue(p) -> float:
            last = self._last_student_photo.get(str(p.user_id))
            return float("-inf") if last is None else last   # never photographed first

        here = sorted((p for p in on_screen if eligible(p)), key=overdue)
        if not here:
            return
        everyone = sorted(
            (p for p in participants.values()
             if p.video_on and not p.is_hold
             and not self._is_self(p.user_id, p.name, ctx) and eligible(p)),
            key=overdue)
        trip = (self._last_trip or {}).get("seconds") or 2 * self.interval_seconds
        waited = now - self._photo_owed_since
        if everyone and overdue(everyone[0]) < overdue(here[0]) and waited < trip + self.interval_seconds:
            return      # someone waiting longer is on another page: their turn is coming
        p = here[0]
        uid = str(p.user_id)
        try:
            shot = await self.client.capture_user(p.user_id)
        except Exception as e:
            logger.warning("student photo failed for %s: %s", p.user_id, e)
            shot = None
        if not shot:
            self._photo_retry_at[uid] = time.monotonic() + self.POLAROID_MIN_GAP_SECONDS
            return
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        folder = f"LiveTutor {ctx.session_label}".strip()
        try:
            # The folder is named for the person and carries their Zoom id,
            # so two students with the same display name never share one.
            await self.storage.upload(
                data=shot,
                filename=f"{_safe(p.name)}_{ts}.png",
                session_folder=folder,
                subfolder=f"{_safe(p.name)}_{_safe(uid)}")
        except Exception as e:
            logger.warning("student photo upload failed for %s: %s", p.user_id, e)
            return
        self._last_student_photo[uid] = time.monotonic()
        self._photo_owed_since = None

    async def _room_picture(self, ctx: CaptureContext) -> None:
        """A picture of the page on screen, each gallery page on its own
        clock, so the pictures cover the whole class within the window
        rather than the first page over and over."""
        if self.room_snapshot_seconds <= 0:
            return
        now = time.monotonic()
        key = self._page_index if self._paged else 0
        last = self._room_shot_at.get(key)
        if last is not None and now - last < self.room_snapshot_seconds:
            return
        try:
            shot = await self.client.page_screenshot()
        except Exception as e:
            logger.warning("room picture failed: %s", e)
            return
        if not shot:
            return
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        page = f"_page{key + 1}" if self._paged else ""
        try:
            await self.storage.upload(
                data=shot,
                filename=f"room_{_safe(ctx.session_label)}{page}_{ts}.png",
                session_folder=f"LiveTutor {ctx.session_label}".strip())
        except Exception as e:
            logger.warning("room picture upload failed: %s", e)
            return
        self._room_shot_at[key] = now
        self._last_room_shot = now

    def gallery_report(self) -> dict:
        """How the gallery walk is going, for the console and /memz: whether
        the gallery has pages, which is on screen, how many turns worked and
        failed, and the last full trip round it (how long it took, how many
        pages, and how many of the cameras were on screen at some point)."""
        return {
            "lookout": self.lookout,
            "paged": self._paged,
            "page": self._page_index + 1,
            "turns": self._turns_ok,
            "failures": self._turn_failures,
            "last_turn": self._last_turn,
            "last_trip": self._last_trip,
            "seen_this_trip": len(self._trip_seen),
            "paused_for_memory": self._throttled,
        }

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

    async def _sweep(self, ctx: CaptureContext, *, turn: bool = False) -> List[dict]:
        """One observation of the room: the page on screen gets its pixel
        work, the page turns when `turn` (the loop decides), then everyone's
        attendance row is read and sent in one request."""
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
        captured_at = snapshot.at or time.time()

        # The valve: under memory pressure, pixel work waits and attendance
        # continues. Unchecked people are recorded as not checked, the
        # same honesty rule as a tile that never rendered. Escalation also
        # runs from the watchdog between sweeps; recovery only here, with
        # hysteresis, so it cannot flap.
        await self._pressure_escalate()
        if self._throttled and self.memory_fraction() < self.MEM_RESUME_BELOW:
            self._throttled = False
            logger.info("[CAPTURE] memory back under %d%%; face captures resume",
                        int(self.MEM_RESUME_BELOW * 100))

        # A student photo falls due once per observation, and goes to the
        # person longest overdue when their page comes up (_student_photo).
        if self.student_photo_seconds > 0 and self._photo_owed_since is None:
            self._photo_owed_since = time.monotonic()

        # The page on screen has its turn's pixel work now, before the read,
        # so what it produces is counted in this observation.
        await self._pass(ctx, participants, await self._watcher_users(arm=True))
        # Asked before the sweep began; the valve above may have closed since.
        # A turn puts a new page of video in front of the decoder, so under
        # pressure the gallery holds still with the rest of the pixel work.
        if turn and not self._throttled and not self.lookout:
            await self._turn(ctx)

        # The read. A face reading counts once: made since the previous
        # observation and not counted before. The mark is taken before the
        # readings are fetched, so one made while they are fetched is counted
        # here and never again, and none falls between two observations.
        read_mark_ms = time.time() * 1000
        since_ms = (self._prev_read_ms if self._prev_read_ms is not None
                    else read_mark_ms - self.FIRST_READ_WINDOW_SECONDS * 1000)
        wusers = await self._watcher_users(arm=False)

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

            # None means "never looked", which is not the same claim as False,
            # "looked and saw nobody". Sending False for an unexamined frame
            # made every tick read downstream as a completed face check, so a
            # report could show 0 of 27 checks failing when no check ever ran.
            reading = self._reading(row.user_id, wusers, since_ms) if video_on else None
            face: Optional[bool] = None
            if reading is not None:
                face = reading[1]
                uid = str(row.user_id)
                self._counted_ms[uid] = reading[0]
                self._last_face_ms[uid] = reading[0]

            rows.append({
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
                "face_checked": face is not None,
                "is_host": is_host,
                "is_cohost": is_cohost,
            })

        # The screenshot records of the checks made since the last
        # observation go now, stamped with this observation's time and
        # saying what its row says. The backend files each record as a
        # sample of that person at its time, so this lands on the same
        # sample as the row that follows, as it always did: stamped at the
        # moment of the check it would be a second sample of them between
        # two observations, and their face result counted twice. A record
        # for someone with no row now (gone to the waiting room) is not
        # sent, or it would put them in the meeting at this time.
        for r in rows:
            shot = self._pending_shots.pop(str(r["participant_id"]), None)
            if shot is None:
                continue
            await self.backend.post_screenshot({
                "session_ref": ctx.session_ref,
                "runtime_id": ctx.runtime_id,
                "participant_id": r["participant_id"],
                "participant_name": r["participant_name"],
                "registrant_id": None,
                "captured_at": captured_at,
                "present": r["present"],
                "video_on": r["video_on"],
                "face_present": r["face_present"],
                **shot,
                "is_host": r["is_host"],
                "is_cohost": r["is_cohost"],
            })
        self._pending_shots.clear()

        # The whole sweep's attendance goes in one request. Per-person posts
        # made the wire the bottleneck: 25 people at a one second pace was
        # 25 sequential round trips per sweep.
        if rows:
            await self.backend.post_attendance_batch(
                session_ref=ctx.session_ref, runtime_id=ctx.runtime_id,
                captured_at=captured_at, rows=rows)
        self._prev_read_ms = read_mark_ms
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
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                if self._rearm_only:
                    # The wake was an interval change, not a request to observe.
                    self._rearm_only = False
                else:
                    try:
                        async with self._sweep_lock:
                            await self._sweep(ctx, turn=self._turn_due_at_read())
                    except Exception as e:  # keep the loop alive across transient errors
                        logger.warning("[CAPTURE] run_once error: %s", e)
                await self._between_sweeps(ctx, started)
        finally:
            watchdog.cancel()
        logger.info("[CAPTURE] attendance loop stopped for session %s", ctx.session_ref)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()          # do not sit out the interval before exiting
