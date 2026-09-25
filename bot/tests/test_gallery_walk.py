"""
The gallery walk: every face gets its turn.

Only a tile on screen can have its face checked, and Zoom's gallery shows
one page at a time: at the bot's size four tiles, one of them the bot. These
tests sit whole classes on a fake clock, through the real capture loop,
against FakeMeetingClient's Zoom-shaped gallery (its rules read out of the
6.2.0 kit), and count who got checked.

FOUND 2026-09-25 (owner: face checks were "not even between participants"):
the bot never turned a page, because it looked for buttons saying "next
page" and Zoom's say "Increase page". Every face check, room picture and
student photo came from the first page. The first test reproduces that with
turn_works=False; the rest pin the walk that replaced it.

Run from the repo root:  python -m pytest bot/tests/test_gallery_walk.py
"""

from __future__ import annotations

import asyncio
import logging
import random
import time as _real_time
from collections import Counter

import pytest

import bot.capture as capture_mod
import bot.meeting_client as client_mod
from bot.capture import CaptureContext, CaptureLoop
from bot.meeting_client import FakeMeetingClient, Participant
from bot.storage import NullStorage, Storage

BOT_ID = "99"
CTX = CaptureContext(runtime_id="r", session_ref="142", meeting_id="m",
                     session_label="botsession:142", bot_name="AALB Assistant")


class FakeClock:
    """Wall and monotonic time that move only when the class does."""

    def __init__(self):
        self.wall = 1_790_000_000.0
        self.mono = 50_000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def strftime(self, fmt, t=None):
        return _real_time.strftime(fmt, _real_time.gmtime(self.wall if t is None else t))

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds

    def __getattr__(self, name):
        return getattr(_real_time, name)


class Memory:
    """The container's memory, as the loop reads it."""
    level = 0.30


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    Memory.level = 0.30
    monkeypatch.setattr(capture_mod, "time", c)
    monkeypatch.setattr(client_mod, "time", c)
    monkeypatch.setattr(CaptureLoop, "memory_fraction",
                        classmethod(lambda cls, **kw: Memory.level))
    return c


class Backend:
    def __init__(self):
        self.batches = []       # one list of rows per observation
        self.stamped = []       # (the observation's time, its rows)
        self.shots = []

    async def post_attendance_batch(self, *, session_ref, runtime_id, captured_at, rows):
        self.batches.append(list(rows))
        self.stamped.append((captured_at, list(rows)))

    async def post_screenshot(self, record):
        self.shots.append(record)

    async def post_event(self, event):
        pass

    def checks(self) -> Counter:
        """How many observations counted a face result for each person."""
        c = Counter()
        for rows in self.batches:
            for r in rows:
                if r["face_checked"]:
                    c[r["participant_id"]] += 1
        return c


class Uploads(Storage):
    def __init__(self):
        self.files = []

    @property
    def stores_images(self):
        return True

    async def upload(self, *, data, filename, session_folder, subfolder=None):
        self.files.append({"filename": filename, "subfolder": subfolder})
        return ("fid", "https://drive/" + filename)


class Gallery(FakeMeetingClient):
    """The fake gallery, keeping a record of every tile photographed."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.captured = []      # (user id, page) of every capture_user that found a tile

    async def capture_user(self, user_id):
        shot = await super().capture_user(user_id)
        if shot:
            self.captured.append((str(user_id), self.page))
        return shot


def a_class(people=22, cameras_off=4, host=None):
    """A class the size of Session 142: 22 students, most with cameras on."""
    return [Participant(str(1000 + i), f"Student {i + 1}",
                        video_on=i < people - cameras_off,
                        is_host=(str(1000 + i) == host))
            for i in range(people)]


def cameras(parts):
    return {p.user_id for p in parts if p.video_on}


def sit(loop, clock, minutes, *, every=None, each_rest=None):
    """The real capture loop, for `minutes` of class on the fake clock.

    Only the rest is replaced: instead of waiting it moves the clock, and
    `each_rest` (when given) runs whatever the class does meanwhile. The
    page is up for a few seconds before the first observation, as it is
    when the bot has just walked in."""
    clock.advance(5)
    end = clock.monotonic() + minutes * 60

    async def rest_until(deadline):
        if loop._stop.is_set():
            return True
        if deadline > clock.monotonic():
            clock.advance(deadline - clock.monotonic())
        if each_rest is not None:
            each_rest(clock)
        if clock.monotonic() >= end:
            loop.stop()
            return True
        woke = loop._wake.is_set()
        loop._wake.clear()
        return woke

    loop._rest_until = rest_until
    asyncio.run(loop.run(CTX))


def make(parts, *, interval=30, watch=True, turn_works=True, page_size=4,
         frames=None, storage=None, lookout=False, room=0, photos=0, backend=None):
    client = Gallery(participants=parts, self_id=BOT_ID, page_size=page_size,
                     turn_works=turn_works, watch=watch,
                     frames=frames if frames is not None else {p.user_id: b"FACE" for p in parts})
    backend = backend or Backend()
    loop = CaptureLoop(client, backend, storage or NullStorage(),
                       interval_seconds=interval, store_images=False,
                       room_snapshot_seconds=room, student_photo_seconds=photos,
                       lookout=lookout, face_detector=lambda data: True)
    return client, backend, loop


# ── What the owner saw ──────────────────────────────────────────────────


def test_a_gallery_that_never_turns_checks_the_same_three_students(clock, caplog):
    """The fault as it was: every turn refused, so the three students on the
    first page got every check and the other fifteen got none. The failure
    is kept where the console can read it, and said in the log without
    flooding it."""
    parts = a_class()
    client, backend, loop = make(parts, turn_works=False)
    with caplog.at_level(logging.WARNING, logger="bot.capture"):
        sit(loop, clock, 30)

    checked = backend.checks()
    assert set(checked) == {"1000", "1001", "1002"}, checked
    assert len(cameras(parts) - set(checked)) == 15

    report = loop.gallery_report()
    assert report["failures"] == len(client.turn_log) > 0
    assert report["turns"] == 0 and report["last_turn"]["ok"] is False
    assert "page buttons" in report["last_turn"]["why"]
    said = [r.getMessage() for r in caplog.records
            if "could not turn the gallery page" in r.getMessage()]
    assert said, "a gallery that will not turn is said in the log"
    assert "Increase page" in said[0], "with the buttons that were on the page"
    assert len(said) <= 31, "at most once a minute"


# ── The walk ────────────────────────────────────────────────────────────


def test_every_camera_gets_an_even_share_of_face_checks(clock):
    """Session 142's shape at the default pace: 22 students, 18 cameras on,
    four tiles a page. Over half an hour each of the 18 is checked the same
    number of times, give or take one, and nobody with a camera off is."""
    parts = a_class()
    client, backend, loop = make(parts, interval=30)
    sit(loop, clock, 30)

    checked = backend.checks()
    per_camera = [checked.get(uid, 0) for uid in cameras(parts)]
    assert min(per_camera) >= 30, checked
    assert max(per_camera) - min(per_camera) <= 1, checked
    assert not set(checked) - cameras(parts), "a camera that is off is never face checked"

    # Every observation still records everyone, checked or not.
    assert all(len(rows) == len(parts) for rows in backend.batches)

    report = loop.gallery_report()
    assert report["paged"] is True and report["failures"] == 0
    trip = report["last_trip"]
    assert trip["cameras"] == trip["seen"] == 18
    assert trip["pages"] == 5, "the page of cameras-off tiles is not visited"
    assert 45 <= trip["seconds"] <= 55, "each page has ten seconds on screen"
    # Pages go 1 to 5 and back to 1, and never skip one.
    walk = "".join("n" if d == "next" else "f" for d in client.turn_log)
    assert "nnnnf" * 30 in walk


def test_a_reading_counts_once(clock):
    """A face reading counts at the first observation after it was made, and
    never again: the people read just before an observation turns the page
    are counted at that observation, not also at the next."""
    parts = a_class()
    client, backend, loop = make(parts, interval=30)
    sit(loop, clock, 2)

    first_page = {"1000", "1001", "1002"}
    counted = [{r["participant_id"] for r in rows if r["face_checked"]} for rows in backend.batches]
    assert counted[0] == first_page
    assert not counted[1] & first_page, counted[1]
    # Each observation counts only the pages shown since the one before.
    assert all(1 <= len(c) <= 12 for c in counted)


def test_a_fast_pace_still_reaches_everyone_evenly(clock):
    """At one observation a second the page turns every ten seconds, the
    shortest turn a page gets, and every camera still comes round equally
    often: nobody is more than one page turn ahead of anyone else."""
    parts = a_class()
    client, backend, loop = make(parts, interval=1)
    sit(loop, clock, 10)

    checked = backend.checks()
    per_camera = [checked.get(uid, 0) for uid in cameras(parts)]
    one_turn = CaptureLoop.PAGE_FLIP_MIN_GAP_SECONDS
    assert min(per_camera) >= 90, checked
    assert max(per_camera) - min(per_camera) <= one_turn + 1, checked
    gaps = len(client.turn_log)
    assert 55 <= gaps <= 61, f"a turn every ten seconds, got {gaps} in ten minutes"


def test_without_the_seat_watcher_screenshots_take_the_same_turns(clock):
    """When the in-page watcher is not running (memory too high to arm it,
    or its detector failed), the face results come from screenshots of the
    tiles on screen. The walk gives them the same even turns, and only a
    tile on screen is ever photographed."""
    parts = a_class()
    client, backend, loop = make(parts, interval=30, watch=False)
    sit(loop, clock, 30)

    checked = backend.checks()
    per_camera = [checked.get(uid, 0) for uid in cameras(parts)]
    assert min(per_camera) >= 30, checked
    assert max(per_camera) - min(per_camera) <= 1, checked
    assert len({uid for uid, _ in client.captured}) == 18

    # The backend files a screenshot record as a sample of that person at
    # the record's time, so a record goes with the observation that counts
    # its check, stamped with that observation's time and saying what its
    # row says. Stamped at the moment of the check it was a second sample
    # of the person between two observations, and their face counted twice.
    samples = {(r["participant_id"], at): r for at, rows in backend.stamped for r in rows}
    for shot in backend.shots:
        row = samples[(shot["participant_id"], shot["captured_at"])]
        assert shot["face_present"] is row["face_present"] is True
        assert shot["video_on"] is row["video_on"] and shot["present"] is row["present"]
    unsent = len(client.captured) - len(backend.shots)
    assert 0 <= unsent <= 8, "only the checks after the last observation go unsent"


def test_a_class_that_fits_one_page_is_checked_every_observation(clock):
    """Three students and the bot fit on one page: nothing to turn, everyone
    read at every observation, and the bot looks for pages once a minute in
    case the class grows."""
    parts = a_class(people=3, cameras_off=0)
    client, backend, loop = make(parts, interval=30)
    sit(loop, clock, 10)

    checked = backend.checks()
    assert set(checked.values()) == {len(backend.batches)}, checked
    assert loop.gallery_report()["paged"] is False
    assert 9 <= len(client.turn_log) <= 11, "one look for pages a minute"


def test_a_class_that_grows_past_one_page_starts_turning(clock):
    """Late arrivals push the class past one page: within a minute the bot
    finds the pages and walks them, and the late arrivals are checked too."""
    parts = a_class(people=3, cameras_off=0)
    late = [Participant(str(2000 + i), f"Late {i + 1}", video_on=True) for i in range(9)]
    client, backend, loop = make(parts, interval=30)

    def arrive(clk):
        if clk.monotonic() >= 50_000 + 5 + 300 and late[0] not in client._participants:
            client._participants.extend(late)

    sit(loop, clock, 15, each_rest=arrive)

    checked = backend.checks()
    assert all(checked.get(p.user_id, 0) >= 10 for p in late), checked
    assert loop.gallery_report()["paged"] is True


def test_the_walk_survives_a_class_that_moves(clock):
    """A real class moves: hands go up and come down (Zoom puts a raised
    hand first, which shifts every tile after it), cameras go off and on,
    people leave and come back. Over an hour, everyone whose camera stayed
    on is still checked, and within a steady share of each other."""
    rng = random.Random(142)
    parts = a_class(people=24, cameras_off=4)
    steady = {p.user_id for p in parts[:10]}      # cameras on all class
    movers = [p for p in parts[10:20]]
    client, backend, loop = make(parts, interval=30)
    left = []

    def the_class_moves(clk):
        roll = rng.random()
        if roll < 0.25 and movers:
            p = rng.choice(movers)
            p.video_on = not p.video_on
        elif roll < 0.45:
            uid = rng.choice([p.user_id for p in client._participants])
            client.raised ^= {uid}
        elif roll < 0.55 and movers:
            p = rng.choice(movers)
            if p in client._participants:
                client._participants.remove(p)
                left.append(p)
        elif roll < 0.65 and left:
            client._participants.append(left.pop(0))

    sit(loop, clock, 60, each_rest=the_class_moves)

    checked = backend.checks()
    per_steady = [checked.get(uid, 0) for uid in steady]
    assert min(per_steady) >= 0.7 * (sum(per_steady) / len(per_steady)), checked
    # Nobody is ever counted while their camera is off or while they are gone.
    for rows in backend.batches:
        for r in rows:
            assert not (r["face_checked"] and not r["video_on"]), r
    report = loop.gallery_report()
    assert report["failures"] == 0 and report["last_trip"] is not None


# ── Holding still ───────────────────────────────────────────────────────


def test_under_memory_pressure_the_gallery_holds_still(clock):
    """Near the memory limit the pixel work pauses and so do the pages: a
    new page is a new set of videos to decode. Attendance goes on at every
    observation, and the walk picks up again once memory comes back."""
    parts = a_class()
    client, backend, loop = make(parts, interval=30)
    Memory.level = 0.90
    start = clock.monotonic()
    before = {}

    def memory_comes_back(clk):
        if not before and clk.monotonic() - start >= 600:
            before.update(turns=list(client.turn_log), captured=list(client.captured),
                          batches=len(backend.batches), checks=backend.checks())
            Memory.level = 0.50

    sit(loop, clock, 20, each_rest=memory_comes_back)

    assert before["turns"] == [], "no page turns under pressure"
    assert before["captured"] == [], "and no screenshots"
    assert before["batches"] == 20, "attendance goes on at every observation"
    assert all(len(rows) == len(parts) for rows in backend.batches)
    assert client.turn_log, "the walk resumes once memory comes back"
    since = backend.checks() - before["checks"]
    assert all(since.get(uid, 0) >= 10 for uid in cameras(parts)), since


def test_a_lookout_never_turns_a_page(clock):
    """A lookout decodes no video it does not need, so it never turns the
    gallery, never reads a face, and never photographs anyone."""
    parts = a_class()
    client, backend, loop = make(parts, interval=30, lookout=True)
    sit(loop, clock, 10)
    assert client.turn_log == []
    assert client.captured == []
    assert backend.checks() == Counter()
    assert len(backend.batches) == 20


# ── Pictures ────────────────────────────────────────────────────────────


def test_room_pictures_cover_every_page(clock):
    """A room picture is of the page on screen, each page on its own clock,
    so over a window the pictures show the whole class rather than the
    first page over and over."""
    parts = a_class()
    uploads = Uploads()
    client, backend, loop = make(parts, interval=30, room=120, storage=uploads)
    sit(loop, clock, 30)

    shot_pages = Counter(client.page_screenshot_pages)
    assert set(shot_pages) == {0, 1, 2, 3, 4}, shot_pages
    assert min(shot_pages.values()) >= 10, shot_pages
    assert max(shot_pages.values()) - min(shot_pages.values()) <= 1, shot_pages
    names = [f["filename"] for f in uploads.files if f["filename"].startswith("room_")]
    assert len(names) == sum(shot_pages.values())
    assert {n.split("_page")[1][0] for n in names if "_page" in n} == {"1", "2", "3", "4", "5"}


def test_student_photos_take_turns_across_every_page(clock):
    """One student photo per observation, to whoever in the whole class has
    waited longest, taken when their page comes up. Over half an hour every
    student with a camera on has the same number of photos, give or take
    one; the trainer (the host) has none; each lands in its own folder."""
    parts = a_class(host="1000")
    uploads = Uploads()
    client, backend, loop = make(parts, interval=30, photos=30, storage=uploads)
    sit(loop, clock, 30)

    photos = Counter(f["subfolder"].rsplit("_", 1)[1] for f in uploads.files if f["subfolder"])
    students = cameras(parts) - {"1000"}
    assert "1000" not in photos, "the host is never photographed"
    per_student = [photos.get(uid, 0) for uid in students]
    assert min(per_student) >= 2, photos
    assert max(per_student) - min(per_student) <= 1, photos
    assert sum(photos.values()) <= len(backend.batches), "at most one photo per observation"
    assert sum(photos.values()) >= 0.75 * len(backend.batches)
