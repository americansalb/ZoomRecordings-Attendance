"""
Bot orchestration tests (no real Zoom needed).

Run from the repo root:  python -m bot.tests.test_bot
Covers: SDK signature, the capture->attribute->manifest pipeline, and the
HTTP contract (join/announce/inbound-chat/send/leave) with fakes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from bot.config import Config
from bot.signature import meeting_sdk_signature
from bot.capture import CaptureLoop, CaptureContext
from bot.manager import BotManager, BotSession
from bot.meeting_client import FakeMeetingClient, Participant
from bot.storage import NullStorage, Storage
from bot.app import build_app


class FakeBackend:
    def __init__(self):
        self.events = []
        self.shots = []
        self.attendance = []

    async def post_event(self, e):
        self.events.append(e)

    async def post_screenshot(self, r):
        self.shots.append(r)

    async def post_attendance(self, r):
        self.attendance.append(r)

    async def post_attendance_batch(self, *, session_ref, runtime_id, captured_at, rows):
        self.batches = getattr(self, "batches", 0) + 1
        self.attendance.extend(rows)


class FakeDriveStorage(Storage):
    @property
    def stores_images(self):
        return True

    async def upload(self, *, data, filename, session_folder, subfolder=None):
        return ("fid-" + filename, "https://drive/" + filename)


def test_signature():
    sig = meeting_sdk_signature("KEY", "SECRET", "98765", role=0)
    h, p, s = sig.split(".")
    expected = base64.urlsafe_b64encode(
        hmac.new(b"SECRET", f"{h}.{p}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert s == expected, "HMAC signature mismatch"
    payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    assert payload["mn"] == "98765" and payload["role"] == 0
    assert payload["sdkKey"] == "KEY" and payload["exp"] > payload["iat"]
    print("  signature OK")


async def _capture_pipeline():
    parts = [
        Participant("1", "Maria Gomez", video_on=True),
        Participant("2", "Sam Lee", video_on=False),
        Participant("99", "AALB Assistant", video_on=True),  # the bot itself
    ]
    client = FakeMeetingClient(participants=parts, frames={"1": b"FACEDATA"},
                               self_id="99")
    backend = FakeBackend()
    detector = lambda data: data == b"FACEDATA"  # noqa: E731
    ctx = CaptureContext(runtime_id="r", session_ref="5", meeting_id="m",
                         session_label="5", bot_name="AALB Assistant")

    # log-only (no image storage)
    loop = CaptureLoop(client, backend, NullStorage(), interval_seconds=300,
                       store_images=False, face_detector=detector)
    rows = await loop.run_once(ctx)
    assert len(rows) == 2, "bot itself must be skipped"
    by_name = {r["participant_name"]: r for r in rows}
    maria, sam = by_name["Maria Gomez"], by_name["Sam Lee"]
    assert maria["video_on"] is True and maria["face_present"] is True
    assert maria["participant_id"] == "1"           # attributed by Zoom id
    assert sam["video_on"] is False
    # No frame was examined for Sam, so face_present is None. False would be a
    # claim we looked and saw nobody, and downstream counts checks by
    # "face_present is not null": sending False invents a check that never ran.
    assert sam["face_present"] is None and sam["face_checked"] is False
    # Manifests account for captured pixels; Sam had no frame, so no
    # manifest. His attendance row above is the record of his tick.
    assert len(backend.shots) == 1
    assert backend.shots[0]["participant_name"] == "Maria Gomez"
    # attendance is reported alongside the manifest, with durations
    assert len(backend.attendance) == 2
    att = {r["participant_name"]: r for r in backend.attendance}
    assert att["Maria Gomez"]["video_on_seconds"] == 60
    assert att["Maria Gomez"]["present"] is True
    assert att["Sam Lee"]["video_on_seconds"] == 0
    assert backend.shots[0]["stored"] is False

    # store-to-Drive path
    backend2 = FakeBackend()
    loop2 = CaptureLoop(client, backend2, FakeDriveStorage(), interval_seconds=300,
                        store_images=True, face_detector=detector)
    await loop2.run_once(ctx)
    m2 = {r["participant_name"]: r for r in backend2.shots}
    assert m2["Maria Gomez"]["stored"] is True and m2["Maria Gomez"]["image_url"].startswith("https://drive/")
    assert "Sam Lee" not in m2, "no frame means no manifest, and nothing uploaded"
    print("  capture pipeline OK")


async def _attendance_without_frames():
    """Attendance must not depend on frame capture.

    The Component View SDK exposes no per-user video stream, so capture_user
    returns None on the real client. Presence and camera state still have to
    produce a full attendance record.
    """
    parts = [Participant("1", "Maria Gomez", video_on=True),
             Participant("2", "Sam Lee", video_on=False)]
    client = FakeMeetingClient(participants=parts, frames={}, self_id="99")
    assert await client.capture_supported() is False
    backend = FakeBackend()
    ctx = CaptureContext(runtime_id="r", session_ref="5", meeting_id="m",
                         session_label="5", bot_name="AALB Assistant")
    loop = CaptureLoop(client, backend, NullStorage(), interval_seconds=300,
                       store_images=True)
    rows = await loop.run_once(ctx)
    assert len(rows) == 2, "attendance is recorded with no frames at all"
    att = {r["participant_name"]: r for r in backend.attendance}
    assert att["Maria Gomez"]["video_on"] is True
    assert att["Maria Gomez"]["face_checked"] is False, "no frame means no face claim"
    # None, not False. This is the real SDK's behaviour on every tick, and
    # reporting False here is what made a report show "0 of 27" face checks
    # when not one check had run.
    assert att["Maria Gomez"]["face_present"] is None
    assert att["Maria Gomez"]["observed_seconds"] == 60
    assert all(s["stored"] is False for s in backend.shots)
    print("  attendance without frames OK")


async def _self_skip_by_id_and_name():
    """The bot is skipped by user id AND by display name.

    The name check is not a fallback. Live, the SDK's current-user id and
    the roster's id for the bot differed, so an id-only rule recorded the
    bot as an attendee and queued a camera message to itself. Skipping by
    name accepts a smaller risk (a student would have to be named exactly
    like the bot) to remove that failure entirely.
    """
    parts = [Participant("1", "Maria Gomez", video_on=True),      # a real student
             Participant("99", "AALB Assistant", video_on=True)]  # the bot, roster id
    # self_id deliberately differs from the roster row, as observed live.
    client = FakeMeetingClient(participants=parts, self_id="12345")
    backend = FakeBackend()
    ctx = CaptureContext(runtime_id="r", session_ref="5", meeting_id="m",
                         session_label="5", bot_name="AALB Assistant")
    loop = CaptureLoop(client, backend, NullStorage(), interval_seconds=300,
                       store_images=False)
    rows = await loop.run_once(ctx)
    ids = {r["participant_id"] for r in rows}
    assert ids == {"1"}, f"the bot must never be recorded as an attendee, got {ids}"
    print("  self-skip by id and name OK")


def test_capture():
    asyncio.run(_capture_pipeline())
    asyncio.run(_attendance_without_frames())
    asyncio.run(_self_skip_by_id_and_name())


def test_contract():
    created = []

    def client_factory(page_url, headless):
        c = FakeMeetingClient(participants=[Participant("1", "Maria", video_on=True)])
        created.append(c)
        return c

    cfg = Config(
        backend_url="http://backend", bot_shared_secret=None,
        sdk_key="KEY", sdk_secret="SECRET",
        public_base_url="http://bot", headless=True, drive_folder_id=None,
    )
    backend = FakeBackend()
    app = build_app(cfg, backend=backend, client_factory=client_factory,
                    storage_factory=lambda store, folder: NullStorage())
    c = TestClient(app)

    # join with announce + capture disabled
    r = c.post("/bots", json={
        "meeting_id": "98765", "session_ref": "7", "display_name": "AALB Assistant",
        "announce": True, "announcement": "Hi class!", "capture": {"enabled": False},
    })
    assert r.status_code == 200, r.text
    rid = r.json()["runtime_id"]
    assert rid.startswith("bot_")
    client0 = created[-1]
    assert client0.joined is True
    assert client0.sent_chats and client0.sent_chats[0]["text"] == "Hi class!", "announcement posted"

    # inbound chat -> normalized backend event
    asyncio.run(client0.inject_chat({
        "message": "when does class start?",
        "sender": {"name": "Maria", "userId": "1"},
        "isPrivate": False,
    }))
    chat_events = [e for e in backend.events if e["type"] == "chat"]
    assert chat_events and chat_events[-1]["text"] == "when does class start?"
    assert chat_events[-1]["channel"] == "public" and chat_events[-1]["participant_name"] == "Maria"
    assert chat_events[-1]["session_ref"] == "7"

    # send a public message
    assert c.post(f"/bots/{rid}/messages", json={"channel": "public", "text": "Welcome!"}).status_code == 200
    assert client0.sent_chats[-1]["text"] == "Welcome!" and client0.sent_chats[-1]["to"] is None
    # send a DM
    assert c.post(f"/bots/{rid}/messages", json={"channel": "dm", "text": "hi", "to_participant_id": "1"}).status_code == 200
    assert client0.sent_chats[-1]["to"] == "1"

    # missing SDK creds -> 400
    cfg2 = Config("http://b", None, None, None, "http://bot", True, None)
    app2 = build_app(cfg2, backend=FakeBackend(), client_factory=client_factory,
                     storage_factory=lambda s, f: NullStorage())
    c2 = TestClient(app2)
    assert c2.post("/bots", json={"meeting_id": "1", "session_ref": "1", "display_name": "B"}).status_code == 400

    # lifecycle: the backend is told we are in, so it can trust the session
    joined_events = [e for e in backend.events if e["type"] == "joined"]
    assert joined_events and joined_events[-1]["runtime_id"] == rid
    assert joined_events[-1]["session_ref"] == "7"

    # leave
    assert c.request("DELETE", f"/bots/{rid}").status_code == 200
    assert client0.joined is False
    print("  contract OK")


def test_meeting_end_is_reported():
    """When Zoom ends the meeting the bot must say so and tear itself down.

    Without this the headless browser sits in a dead meeting and the backend
    keeps the session marked active forever.
    """
    async def scenario():
        created = []

        def client_factory(page_url, headless):
            c = FakeMeetingClient(participants=[Participant("1", "Maria")], self_id="99")
            created.append(c)
            return c

        cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                     sdk_key="KEY", sdk_secret="SECRET",
                     public_base_url="http://bot", headless=True, drive_folder_id=None)
        backend = FakeBackend()
        manager = BotManager(cfg, backend, client_factory=client_factory,
                             storage_factory=lambda s, f: NullStorage())
        rid = await manager.join({"meeting_id": "98765", "session_ref": "7",
                                  "display_name": "AALB Assistant"})
        client = created[-1]
        assert client.joined is True

        # Zoom reports the meeting closed.
        await client.inject_lifecycle("ended", "Closed")
        await asyncio.sleep(0)          # let the detached reap task run
        for _ in range(20):
            if not client.joined:
                break
            await asyncio.sleep(0.05)

        left = [e for e in backend.events if e["type"] == "left"]
        assert left and left[-1]["runtime_id"] == rid, "backend must be told we left"
        assert client.joined is False, "the client must be torn down"
        assert rid not in manager._sessions, "the session must be dropped"

    asyncio.run(scenario())
    print("  meeting-end reaping OK")


def test_capture_now_and_retune():
    """An operator can force a sweep and change the cadence mid-meeting."""
    async def scenario():
        def client_factory(page_url, headless):
            return FakeMeetingClient(
                participants=[Participant("1", "Maria", video_on=True),
                              Participant("2", "Sam", video_on=False)],
                self_id="99")

        cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                     sdk_key="KEY", sdk_secret="SECRET",
                     public_base_url="http://bot", headless=True, drive_folder_id=None)
        backend = FakeBackend()
        manager = BotManager(cfg, backend, client_factory=client_factory,
                             storage_factory=lambda s, f: NullStorage())
        rid = await manager.join({
            "meeting_id": "98765", "session_ref": "7", "display_name": "AALB Assistant",
            "capture": {"interval_seconds": 3600},
        })
        before = len(backend.attendance)

        # Force a sweep without waiting out the hour-long interval.
        recorded = await manager.capture_now(rid)
        assert recorded == 2, recorded
        assert len(backend.attendance) == before + 2

        # Retune the cadence on the running loop.
        cfg2 = manager.set_capture_config(rid, interval_seconds=30)
        assert cfg2["interval_seconds"] == 30
        # The floor is enforced, so a silly value cannot hammer Zoom.
        cfg3 = manager.set_capture_config(rid, interval_seconds=1)
        assert cfg3["interval_seconds"] == CaptureLoop.MIN_INTERVAL_SECONDS

        # Unknown runtime ids are rejected rather than silently ignored.
        try:
            await manager.capture_now("bot_nope")
            raise AssertionError("expected KeyError")
        except KeyError:
            pass

        await manager.leave(rid)

    asyncio.run(scenario())
    print("  capture-now and retune OK")


def test_lists_live_bots():
    """The control plane must be able to ask what is actually running.

    Its own records are written optimistically at join and corrected only by
    an event the bot sends. A bot that died with its container sends nothing,
    so without this the console shows a bot sitting in an empty meeting.
    """
    cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                 sdk_key="KEY", sdk_secret="SECRET",
                 public_base_url="http://bot", headless=True, drive_folder_id=None)
    app = build_app(cfg, backend=FakeBackend(),
                    client_factory=lambda page_url, headless: FakeMeetingClient(self_id="99"),
                    storage_factory=lambda s, f: NullStorage())
    c = TestClient(app)

    assert c.get("/bots").json()["bots"] == []

    rid = c.post("/bots", json={"meeting_id": "98765", "session_ref": "botsession:7",
                                "display_name": "AALB Assistant"}).json()["runtime_id"]
    bots = c.get("/bots").json()["bots"]
    assert len(bots) == 1
    assert bots[0]["runtime_id"] == rid
    assert bots[0]["meeting_id"] == "98765"
    assert bots[0]["session_ref"] == "botsession:7"

    c.request("DELETE", f"/bots/{rid}")
    assert c.get("/bots").json()["bots"] == [], "a departed bot must not still be listed"
    print("  live bot listing OK")


def test_failed_join_cleans_up():
    """A join that raises must not leave a browser (or a ghost) behind.

    This is the failure that produced a bot silently parked in a meeting: the
    SDK connects, a later step throws, and nothing upstream ever learns a
    runtime_id to send a leave to. The manager has to tear the client down
    itself and tell the backend the session errored.
    """
    closed = []

    class ExplodingClient(FakeMeetingClient):
        async def join(self, **kwargs):
            self.joined = True          # the SDK really is in the meeting now
            raise RuntimeError("Zoom join rejected: {\"errorCode\":3712}")

        async def close(self):
            closed.append(True)
            self.joined = False

    def client_factory(page_url, headless):
        return ExplodingClient()

    cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                 sdk_key="KEY", sdk_secret="SECRET",
                 public_base_url="http://bot", headless=True, drive_folder_id=None)
    backend = FakeBackend()
    app = build_app(cfg, backend=backend, client_factory=client_factory,
                    storage_factory=lambda s, f: NullStorage())
    c = TestClient(app)

    r = c.post("/bots", json={"meeting_id": "98765", "session_ref": "7",
                              "display_name": "AALB Assistant"})
    assert r.status_code >= 400, r.text
    # The reason Zoom gave has to survive all the way to the caller, or the
    # operator is left guessing at a generic failure.
    assert "3712" in r.text, r.text
    assert closed, "the browser must be torn down when the join fails"
    errors = [e for e in backend.events if e["type"] == "error"]
    assert errors and "3712" in errors[-1]["error"]
    assert errors[-1]["session_ref"] == "7"
    print("  failed-join cleanup OK")


def test_join_passes_credentials():
    """Passcode, ZAK and role must reach the meeting client."""
    seen = {}

    class RecordingClient(FakeMeetingClient):
        async def join(self, **kwargs):
            seen.update(kwargs)
            self.joined = True

    cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                 sdk_key="KEY", sdk_secret="SECRET",
                 public_base_url="http://bot", headless=True, drive_folder_id=None)
    app = build_app(cfg, backend=FakeBackend(),
                    client_factory=lambda page_url, headless: RecordingClient(),
                    storage_factory=lambda s, f: NullStorage())
    c = TestClient(app)

    # explicit passcode wins
    assert c.post("/bots", json={"meeting_id": "1", "session_ref": "1",
                                 "display_name": "B", "passcode": "abc123",
                                 "zak": "ZAKTOKEN"}).status_code == 200
    assert seen["passcode"] == "abc123"
    assert seen["zak"] == "ZAKTOKEN"

    # falls back to the pwd carried in a join URL
    assert c.post("/bots", json={
        "meeting_id": "1", "session_ref": "1", "display_name": "B",
        "join_url": "https://zoom.us/j/1?pwd=frmUrl",
    }).status_code == 200
    assert seen["passcode"] == "frmUrl"
    assert seen["zak"] is None
    print("  join credentials OK")


async def _face_rotation():
    parts = [Participant(str(i), f"P{i}", video_on=True) for i in range(1, 7)]
    frames = {str(i): b"F" for i in range(1, 7)}

    class CountingClient(FakeMeetingClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.captured: list = []
            self.advances = 0

        async def capture_user(self, user_id):
            self.captured.append(str(user_id))
            return await super().capture_user(user_id)

        async def gallery_advance(self):
            self.advances += 1
            return {"ok": True, "moved": "next"}

    client = CountingClient(participants=parts, frames=frames, self_id="99")
    loop = CaptureLoop(client, FakeBackend(), NullStorage(), interval_seconds=300,
                       store_images=False, face_detector=lambda d: True)
    ctx = CaptureContext(runtime_id="r", session_ref="5", meeting_id="m",
                         session_label="5", bot_name="AALB Assistant")

    first = await loop.run_once(ctx)
    assert len(first) == 6, "everyone is still observed every sweep"
    assert len(client.captured) == CaptureLoop.FACE_CHECKS_PER_SWEEP, \
        "face checks per sweep are capped"
    unchecked = [r for r in first if not r["face_checked"]]
    # Honesty invariant: an unexamined frame is None, never False.
    assert all(r["face_present"] is None for r in unchecked)

    await loop.run_once(ctx)
    assert set(client.captured) == {str(i) for i in range(1, 7)}, \
        "the rotation reaches everyone across sweeps"
    # More cameras than one sweep's cap flips the gallery, but never
    # faster than the dwell gap: two back-to-back sweeps flip once.
    assert client.advances == 1, "back-to-back sweeps stay on the page for the dwell gap"
    loop._last_advance = 0.0
    loop._last_polaroid.clear()
    await loop.run_once(ctx)
    assert client.advances == 2, "after the dwell gap the gallery flips again"


def test_face_rotation():
    asyncio.run(_face_rotation())
    print("  face check rotation OK")


async def _send_dedupe():
    client = FakeMeetingClient()
    mgr = BotManager(config=None, backend=None,
                     client_factory=lambda **k: None,
                     storage_factory=lambda a, b: None)
    mgr._sessions["rt"] = BotSession("rt", "m", "ref", "Bot", client)

    text1 = "Your camera is off. (Reminder #1 for Lalo)"
    assert await mgr.send("rt", "dm", text1, "77") is True
    assert await mgr.send("rt", "dm", text1, "77") is True, \
        "the duplicate is acknowledged as delivered"
    assert len(client.sent_chats) == 1, "the identical resend never reaches the meeting"
    assert await mgr.send("rt", "dm", "Your camera is off. (Reminder #2 for Lalo)", "77") is True
    assert len(client.sent_chats) == 2, "a different text goes out"
    assert await mgr.send("rt", "dm", text1, "88") is True
    assert len(client.sent_chats) == 3, "the same text to a different person goes out"


def test_send_dedupe():
    asyncio.run(_send_dedupe())
    print("  duplicate send suppression OK")


async def _memory_valve():
    parts = [Participant("1", "Maria Gomez", video_on=True)]
    client = FakeMeetingClient(participants=parts, frames={"1": b"F"}, self_id="99")
    loop = CaptureLoop(client, FakeBackend(), NullStorage(), interval_seconds=300,
                       store_images=False, face_detector=lambda d: True)
    ctx = CaptureContext(runtime_id="r", session_ref="5", meeting_id="m",
                         session_label="5", bot_name="AALB Assistant")

    loop.memory_fraction = lambda: 0.90        # over the soft limit
    rows = await loop.run_once(ctx)
    assert rows[0]["present"] is True, "attendance never pauses"
    assert rows[0]["face_checked"] is False and rows[0]["face_present"] is None, \
        "face work pauses under memory pressure, recorded as not checked"

    loop.memory_fraction = lambda: 0.80        # below soft, above resume
    rows = await loop.run_once(ctx)
    assert rows[0]["face_checked"] is False, "hysteresis holds the pause"

    loop.memory_fraction = lambda: 0.50        # well clear
    rows = await loop.run_once(ctx)
    assert rows[0]["face_checked"] is True and rows[0]["face_present"] is True, \
        "face checks resume when memory recovers"


def test_memory_valve():
    asyncio.run(_memory_valve())
    print("  memory pressure valve OK")


async def _watchdog_escalation():
    class ShrinkClient(FakeMeetingClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.shrinks = 0

        async def shrink_viewport(self):
            self.shrinks += 1

    client = ShrinkClient()
    loop = CaptureLoop(client, FakeBackend(), NullStorage(), interval_seconds=300,
                       store_images=False, face_detector=lambda d: True)
    loop.memory_fraction = lambda: 0.95        # past the hard limit
    await loop._pressure_escalate()
    assert loop._throttled is True, "escalation throttles without waiting for a sweep"
    assert client.shrinks == 1, "past the hard limit the window shrinks"
    await loop._pressure_escalate()
    assert client.shrinks == 2, "shrink stays idempotent client-side, escalation retries"


def test_watchdog_escalation():
    asyncio.run(_watchdog_escalation())
    print("  watchdog escalation OK")


async def _watcher_feeds_sweep():
    import time as _t

    class WatchingClient(FakeMeetingClient):
        def __init__(self, wusers, **kw):
            super().__init__(**kw)
            self.wusers = wusers
            self.captured = []

        async def watcher_state(self):
            return {"running": True, "users": self.wusers}

        async def capture_user(self, user_id):
            self.captured.append(str(user_id))
            return await super().capture_user(user_id)

    parts = [Participant("1", "Maria Gomez", video_on=True),
             Participant("2", "Sam Lee", video_on=True)]
    now_ms = _t.time() * 1000
    # Maria has a fresh in-page reading; Sam's is stale, so he must fall
    # back to the screenshot path.
    wusers = {
        "1": {"readable": True, "facePresent": True, "lastCheckedAt": now_ms},
        "2": {"readable": True, "facePresent": True, "lastCheckedAt": now_ms - 60_000},
    }
    client = WatchingClient(wusers, participants=parts, frames={"2": b"F"}, self_id="99")
    loop = CaptureLoop(client, FakeBackend(), NullStorage(), interval_seconds=300,
                       store_images=False, face_detector=lambda d: False)
    ctx = CaptureContext(runtime_id="r", session_ref="5", meeting_id="m",
                         session_label="5", bot_name="AALB Assistant")
    rows = {r["participant_id"]: r for r in await loop.run_once(ctx)}

    assert rows["1"]["face_present"] is True and rows["1"]["face_checked"] is True, \
        "a fresh watcher reading is the face result"
    assert "1" not in client.captured, "watcher coverage takes no screenshot"
    assert "2" in client.captured, "a stale reading falls back to the screenshot path"
    assert rows["2"]["face_present"] is False, "fallback still runs the detector"


def test_watcher_feeds_sweep():
    asyncio.run(_watcher_feeds_sweep())
    print("  watcher feeds the sweep OK")


def run():
    test_signature()
    test_capture()
    test_contract()
    test_meeting_end_is_reported()
    test_capture_now_and_retune()
    test_lists_live_bots()
    test_failed_join_cleans_up()
    test_join_passes_credentials()
    test_face_rotation()
    test_send_dedupe()
    test_memory_valve()
    test_watchdog_escalation()
    test_watcher_feeds_sweep()
    print("BOT TESTS PASSED")


if __name__ == "__main__":
    run()


async def _room_snapshots():
    """The whole-room grid shot moved off the sweep onto its own loop
    (_grid_loop, tested in test_grid_*), so it can walk every gallery page
    fast enough to cover everyone in the window instead of firing at most
    once per sweep. What this pins is that the move is clean: a sweep emits
    no room files itself, and a page that cannot be photographed never
    fails the sweep.
    """
    class RecordingStorage(Storage):
        def __init__(self):
            self.uploads = []

        @property
        def stores_images(self):
            return True

        async def upload(self, *, data, filename, session_folder, subfolder=None):
            self.uploads.append((filename, session_folder, len(data)))
            return ("fid", "https://drive/" + filename)

    class SnappableClient(FakeMeetingClient):
        async def page_screenshot(self):
            return b"PAGEPIXELS"

    parts = [Participant("1", "Maria Gomez", video_on=True)]
    ctx = CaptureContext(runtime_id="r", session_ref="61", meeting_id="m",
                         session_label="botsession:61", bot_name="AALB Assistant")

    # The sweep never emits room files now, on or off: that is the grid loop's job.
    for room_secs in (0, 3600):
        store = RecordingStorage()
        loop = CaptureLoop(SnappableClient(participants=parts), FakeBackend(), store,
                           interval_seconds=300, store_images=False,
                           room_snapshot_seconds=room_secs)
        await loop.run_once(ctx)
        await loop.run_once(ctx)
        assert [u for u in store.uploads if u[0].startswith("room_")] == [], \
            "the sweep itself emits no room shots; the grid loop owns them"

    # A page that cannot be photographed is a skipped shot, not a failed sweep.
    store_broken = RecordingStorage()
    loop3 = CaptureLoop(FakeMeetingClient(participants=parts), FakeBackend(), store_broken,
                        interval_seconds=300, store_images=False,
                        room_snapshot_seconds=3600)
    rows = await loop3.run_once(ctx)
    assert len(rows) == 1 and store_broken.uploads == []


def test_room_snapshots():
    asyncio.run(_room_snapshots())


async def _student_photos():
    """One student at a time, filed in a folder of their own.

    Opt-in. One photo per sweep at most, each person on their own clock,
    the longest overdue going next so a class rotates fairly. Cameras-off
    students are not photographed: there is no tile, and a missing photo
    must never read as a picture of an empty seat.
    """
    class RecordingStorage(Storage):
        def __init__(self):
            self.uploads = []

        @property
        def stores_images(self):
            return True

        async def upload(self, *, data, filename, session_folder, subfolder=None):
            self.uploads.append((filename, session_folder, subfolder))
            return ("fid", "https://drive/" + filename)

    parts = [
        Participant("1", "Maria Gomez", video_on=True),
        Participant("2", "Daniel Reyes", video_on=True),
        Participant("3", "Sam Lee", video_on=False),
    ]
    frames = {"1": b"MARIA", "2": b"DANIEL", "3": b"SAM"}
    ctx = CaptureContext(runtime_id="r", session_ref="61", meeting_id="m",
                         session_label="botsession:61", bot_name="AALB Assistant")

    # Off by default.
    off = RecordingStorage()
    loop = CaptureLoop(FakeMeetingClient(participants=parts, frames=frames),
                       FakeBackend(), off, interval_seconds=300, store_images=False)
    await loop.run_once(ctx)
    assert off.uploads == [], "student photos must be opt-in"

    # On: one per sweep, rotating, and each lands in its own folder.
    store = RecordingStorage()
    loop2 = CaptureLoop(FakeMeetingClient(participants=parts, frames=frames),
                        FakeBackend(), store, interval_seconds=300,
                        store_images=False, student_photo_seconds=3600)
    await loop2.run_once(ctx)
    await loop2.run_once(ctx)
    await loop2.run_once(ctx)
    assert len(store.uploads) == 2, (
        f"one photo per sweep for each camera-on student, got {store.uploads}")
    folders = sorted(u[2] for u in store.uploads)
    assert folders == ["Daniel-Reyes_2", "Maria-Gomez_1"], folders
    assert all(u[1].endswith("botsession:61") for u in store.uploads)
    assert not any("Sam" in (u[2] or "") for u in store.uploads), (
        "a camera-off student has no tile to photograph"
    )

    # A lookout never photographs anyone, whatever it is asked for.
    lookout_store = RecordingStorage()
    loop3 = CaptureLoop(FakeMeetingClient(participants=parts, frames=frames),
                        FakeBackend(), lookout_store, interval_seconds=300,
                        store_images=True, student_photo_seconds=30, lookout=True)
    await loop3.run_once(ctx)
    assert loop3.student_photo_seconds == 0
    assert lookout_store.uploads == []


def test_student_photos():
    asyncio.run(_student_photos())


async def _lookout_mode():
    """A lookout records the room and touches no pixels.

    Presence and camera state come from the one roster read; face checks,
    the watcher, per-person screenshots and room pictures all stay off for
    the life of the session, and a settings change cannot switch pixels
    back on, because the page never rendered enough video for them to mean
    anything.
    """
    class WatcherClient(FakeMeetingClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.watcher_reads = 0
            self.captured = set()

        async def watcher_state(self):
            self.watcher_reads += 1
            return {"running": False}

        async def capture_user(self, user_id):
            self.captured.add(str(user_id))
            return await super().capture_user(user_id)

        async def page_screenshot(self):
            return b"PAGEPIXELS"

    class RecordingStorage(Storage):
        def __init__(self):
            self.uploads = []

        @property
        def stores_images(self):
            return True

        async def upload(self, *, data, filename, session_folder, subfolder=None):
            self.uploads.append(filename)
            return ("fid", "https://drive/" + filename)

    parts = [
        Participant("1", "Maria Gomez", video_on=True),
        Participant("2", "Sam Lee", video_on=False),
    ]
    client = WatcherClient(participants=parts, frames={"1": b"FACEDATA"},
                           self_id="99")
    backend = FakeBackend()
    store = RecordingStorage()
    ctx = CaptureContext(runtime_id="r", session_ref="7", meeting_id="m",
                         session_label="7", bot_name="AALB Assistant")
    # Everything pixel-hungry is asked for, and lookout must win anyway.
    loop = CaptureLoop(client, backend, store, interval_seconds=300,
                       store_images=True, room_snapshot_seconds=60,
                       lookout=True)

    rows = {r["participant_id"]: r for r in await loop.run_once(ctx)}

    # The record is intact: presence and camera state for the whole room.
    assert rows["1"]["video_on"] is True and rows["1"]["present"] is True
    assert rows["2"]["video_on"] is False
    # And honest: no face was checked, and nothing claims one was.
    assert rows["1"]["face_present"] is None
    assert rows["1"]["face_checked"] is False
    # No pixels were touched anywhere.
    assert not client.captured, "a lookout must never screenshot a person"
    assert client.watcher_reads == 0, "a lookout never consults the watcher"
    assert store.uploads == [], "a lookout keeps no pixels"
    assert backend.shots == [], "no pixels means no manifest rows"
    assert loop.store_images is False
    assert loop.room_snapshot_seconds == 0


async def _lookout_is_manager_default():
    """The manager treats lookout as the default and pins it for the session.

    A join that never mentions lookout gets one; the client is told at join
    time (the page sizes its video from that flag); the live listing says
    so; and a later settings call cannot smuggle pixel work back in.
    """
    created = []

    class RecordingJoinClient(FakeMeetingClient):
        async def join(self, **kwargs):
            self.join_kwargs = kwargs
            await super().join(**kwargs)

    def client_factory(page_url, headless):
        c = RecordingJoinClient(
            participants=[Participant("1", "Maria", video_on=True)], self_id="99")
        created.append(c)
        return c

    cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                 sdk_key="KEY", sdk_secret="SECRET",
                 public_base_url="http://bot", headless=True, drive_folder_id=None)
    manager = BotManager(cfg, FakeBackend(), client_factory=client_factory,
                         storage_factory=lambda s, f: NullStorage())

    rid = await manager.join({"meeting_id": "98765", "session_ref": "7",
                              "display_name": "AALB Assistant",
                              "capture": {"interval_seconds": 3600}})
    assert created[-1].join_kwargs["lookout"] is True
    assert manager.list_sessions()[0]["lookout"] is True

    got = manager.set_capture_config(rid, store_images=True,
                                     room_snapshot_seconds=120)
    assert got["lookout"] is True
    assert got["store_images"] is False, "a lookout cannot start keeping frames"
    assert got["room_snapshot_seconds"] == 0, "a lookout cannot start room shots"
    assert "no pixels" in got.get("note", ""), (
        "declining pixel work must be said, not silently succeeded")
    # A plain settings change carries no note: nothing was declined.
    assert "note" not in manager.set_capture_config(rid, interval_seconds=30)
    await manager.leave(rid)

    # Asking to watch video still works: the opt-in survives the gut.
    rid2 = await manager.join({"meeting_id": "98765", "session_ref": "8",
                               "display_name": "AALB Assistant",
                               "capture": {"lookout": False, "enabled": True}})
    assert created[-1].join_kwargs["lookout"] is False
    assert manager.list_sessions()[0]["lookout"] is False
    await manager.leave(rid2)

    # A caller that predates the flag and asks for pixels keeps the watcher
    # it always had: absent lookout defers to capture.enabled.
    rid3 = await manager.join({"meeting_id": "98765", "session_ref": "9",
                               "display_name": "AALB Assistant",
                               "capture": {"enabled": True, "store_images": True}})
    assert created[-1].join_kwargs["lookout"] is False
    await manager.leave(rid3)


def test_lookout_mode():
    asyncio.run(_lookout_mode())
    asyncio.run(_lookout_is_manager_default())


def test_lookout_parsing():
    """The flag's edge cases: JSON null, strings, and legacy callers.

    bool() alone turned an explicit null into the heavy video path and the
    string "false" into a lookout, each the opposite of what was asked.
    """
    from bot.manager import _parse_lookout
    assert _parse_lookout({}) is True
    assert _parse_lookout({"lookout": None}) is True
    assert _parse_lookout({"lookout": None, "enabled": True}) is False
    assert _parse_lookout({"enabled": True}) is False
    assert _parse_lookout({"enabled": False}) is True
    assert _parse_lookout({"lookout": True}) is True
    assert _parse_lookout({"lookout": False, "enabled": False}) is False
    assert _parse_lookout({"lookout": "false"}) is False
    assert _parse_lookout({"lookout": "off"}) is False
    assert _parse_lookout({"lookout": "true"}) is True


def test_browser_death_classifier():
    """Only a dead browser earns the normal-flags retry during a join.

    A Zoom rejection fails identically on any flag set, so retrying it
    would just double the worst-case join time.
    """
    from bot.meeting_client import _looks_like_browser_death
    dead = [
        "Page.evaluate: Target page, context or browser has been closed",
        "Page crashed",
        "Browser has been disconnected",
        "Connection closed while reading from the driver",
    ]
    alive = [
        "Zoom join rejected: wrong passcode",
        "Zoom join failed: admission from the waiting room never completed",
        "Zoom SDK init failed",
    ]
    for msg in dead:
        assert _looks_like_browser_death(RuntimeError(msg)), msg
    for msg in alive:
        assert not _looks_like_browser_death(RuntimeError(msg)), msg


def test_camera_picture_launch_args(monkeypatch):
    """The cat ships with the bot and rides Chromium's fake webcam flag,
    and BOT_CAMERA_FACE=off removes it without a build."""
    from bot.meeting_client import PlaywrightZoomClient
    c = PlaywrightZoomClient(page_url="http://x", headless=True)
    monkeypatch.delenv("BOT_CAMERA_FACE", raising=False)
    assert PlaywrightZoomClient.CAMERA_FACE_FILE.is_file()
    args = c._launch_args(lookout=True)
    assert any(a.startswith("--use-file-for-fake-video-capture=") for a in args)
    assert "--single-process" in args
    assert "--use-fake-device-for-media-stream" in args
    monkeypatch.setenv("BOT_CAMERA_FACE", "off")
    assert not any(a.startswith("--use-file-for-fake-video-capture=")
                   for a in c._launch_args(lookout=False))


def test_camera_picture_yields_under_pressure(monkeypatch):
    """At the memory hard limit the cosmetic camera is the first thing to
    go, and it is asked to go exactly once."""
    stops = []

    class StoppingClient(FakeMeetingClient):
        async def stop_video(self):
            stops.append(1)
            return {"ok": True}

    loop = CaptureLoop(StoppingClient(), FakeBackend(), NullStorage(),
                       interval_seconds=30, store_images=False)
    monkeypatch.setattr(CaptureLoop, "memory_fraction", classmethod(lambda cls: 0.95))
    asyncio.run(loop._pressure_escalate())
    asyncio.run(loop._pressure_escalate())
    assert stops == [1]


def test_pool_picture_becomes_the_camera(tmp_path, monkeypatch):
    """A picture from the console's pool is converted for the fake webcam
    and takes the built-in picture's place in the launch flags; a picture
    that will not decode leaves the built-in one alone."""
    import numpy as np
    import cv2
    from bot.meeting_client import PlaywrightZoomClient, image_bytes_to_y4m

    img = np.zeros((300, 400, 3), dtype=np.uint8)
    img[:, :, 1] = 200
    ok, png = cv2.imencode(".png", img)
    assert ok
    out = tmp_path / "face.y4m"
    assert image_bytes_to_y4m(png.tobytes(), str(out))
    data = out.read_bytes()
    assert data.startswith(b"YUV4MPEG2 W640 H360 F10:1")
    header_len = data.index(b"\n") + 1
    frame_len = 640 * 360 * 3 // 2
    assert len(data) == header_len + 2 * (len(b"FRAME\n") + frame_len)

    monkeypatch.delenv("BOT_CAMERA_FACE", raising=False)
    c = PlaywrightZoomClient(page_url="http://x", headless=True)
    assert c.set_camera_face(png.tobytes())
    args = c._launch_args(lookout=True)
    flag = next(a for a in args if a.startswith("--use-file-for-fake-video-capture="))
    assert flag.endswith(".y4m") and "assets/bot-face.y4m" not in flag
    assert c.set_camera_face(b"not an image") is False
    assert c.set_camera_face(None) is False


def test_camera_picture_memory_gate(monkeypatch):
    """The picture is allowed while the machine has room and declined near
    the line. The limit sits above a lookout's measured resting level in a
    small room (69 percent on the 512 MB machine): a limit at 70 declined
    the picture on ordinary days."""
    from bot.meeting_client import PlaywrightZoomClient
    from bot.capture import CaptureLoop
    monkeypatch.delenv("BOT_CAMERA_FACE", raising=False)
    c = PlaywrightZoomClient(page_url="http://x", headless=True)
    assert c.CAMERA_FACE_MAX_MEM <= 0.65
    monkeypatch.setattr(CaptureLoop, "memory_fraction", classmethod(lambda cls: 0.50))
    wanted, reason = c._camera_face_decision()
    assert wanted and "50 percent" in reason
    monkeypatch.setattr(CaptureLoop, "memory_fraction", classmethod(lambda cls: 0.72))
    wanted, reason = c._camera_face_decision()
    assert not wanted and "72 percent" in reason and "60" in reason


def test_camera_report_carries_zooms_own_words():
    """Diagnostics attach Zoom's errors and warnings about video and the
    camera to the camera picture's report, and nothing else."""
    import asyncio
    from bot.meeting_client import PlaywrightZoomClient

    class FakePage:
        async def evaluate(self, _js):
            return {"cameraFace": {"wanted": True, "phase": "Zoom never reported video on", "notes": []}}

    c = PlaywrightZoomClient(page_url="http://x", headless=True)
    c._page = FakePage()
    c._page_console = [
        "[log] video frame rendered",
        "[error] video encoder init failed: wasm not ready",
        "[warning] getUserMedia NotAllowedError: permission denied",
        "[error] chat socket closed",
        "[error] init tf fail",
    ]
    data = asyncio.run(c.diagnostics())
    notes = data["cameraFace"]["notes"]
    assert notes == [
        "[error] video encoder init failed: wasm not ready",
        "[warning] getUserMedia NotAllowedError: permission denied",
    ]


def test_memory_meter_is_the_working_set(tmp_path, monkeypatch):
    """The meter subtracts the inactive file cache from what the cgroup
    reports, the way docker stats does, and the raw number stays
    available beside it. A missing stat file leaves the raw number."""
    from bot.capture import CaptureLoop
    cur, mx, stat = tmp_path / "memory.current", tmp_path / "memory.max", tmp_path / "memory.stat"
    cur.write_text("480000000\n")
    mx.write_text("536870912\n")
    stat.write_text("anon 300000000\nfile 170000000\ninactive_file 120000000\nactive_file 50000000\n")
    monkeypatch.setattr(CaptureLoop, "MEM_CURRENT_PATHS", (str(cur),))
    monkeypatch.setattr(CaptureLoop, "MEM_MAX_PATHS", (str(mx),))
    monkeypatch.setattr(CaptureLoop, "MEM_STAT_PATHS", (str(stat),))
    assert round(CaptureLoop.memory_fraction(with_cache=True), 3) == round(480000000 / 536870912, 3)
    assert round(CaptureLoop.memory_fraction(), 3) == round(360000000 / 536870912, 3)
    monkeypatch.setattr(CaptureLoop, "MEM_STAT_PATHS", (str(tmp_path / "absent"),))
    assert round(CaptureLoop.memory_fraction(), 3) == round(480000000 / 536870912, 3)


# ── The grid proctor: walk every page so nobody goes unseen ──────────────
def test_grid_interval_covers_everyone_in_the_window():
    """The tick is window/pages so P pages are all shot within the window,
    floored at the flip gap because a page cannot be flipped and painted
    faster. Beyond that floor the achieved coverage honestly slips past
    the target instead of pretending to hit it."""
    gi = CaptureLoop._grid_interval
    assert gi(30, 1, 10) == 30, "one page: a shot every window"
    assert gi(30, 2, 10) == 15, "two pages: every 15s covers both in 30"
    assert gi(30, 3, 10) == 10, "three pages: every 10s covers all in 30"
    assert gi(30, 5, 10) == 10, "five pages: floored at the flip gap"
    # Five pages at a 10s floor is a real 50s to see everyone, not 30.
    assert gi(30, 5, 10) * 5 == 50


class _RecordingStorage:
    def __init__(self):
        self.uploads = 0
        self.names = []

    @property
    def stores_images(self):
        return True

    async def upload(self, *, data, filename, session_folder, subfolder=None):
        self.uploads += 1
        self.names.append(filename)


def _grid_ctx():
    return CaptureContext(runtime_id="r", session_ref="7", meeting_id="m",
                          session_label="Session 142", bot_name="AALB Attendance Bot")


async def _grid_walks_every_page():
    parts = [Participant(str(i), f"Student {i}", video_on=True) for i in range(30)]
    client = FakeMeetingClient(participants=parts, self_id="99")
    client.gallery_pages = 3            # 30 people, ~10 a page
    store = _RecordingStorage()
    loop = CaptureLoop(client, FakeBackend(), store, interval_seconds=300,
                       store_images=True, room_snapshot_seconds=30)
    ticks = {"n": 0}

    async def fast_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 6:
            loop._stop.set()

    loop._grid_sleep = fast_sleep
    await loop._grid_loop(_grid_ctx())
    assert store.uploads >= 3, "photographs each of the pages, not just the first"
    assert getattr(client, "gallery_advances", 0) >= 3, "walks the gallery to reach every page"
    assert loop._grid_pages == 3
    assert loop._grid_cover_seconds == 30.0, "reports the coverage window it actually achieves"


def test_grid_walks_every_page():
    asyncio.run(_grid_walks_every_page())
    print("  grid proctor covers every page OK")


async def _grid_holds_under_pressure():
    client = FakeMeetingClient(
        participants=[Participant("1", "A", video_on=True)], self_id="9")
    client.gallery_pages = 2
    store = _RecordingStorage()
    loop = CaptureLoop(client, FakeBackend(), store, interval_seconds=300,
                       store_images=True, room_snapshot_seconds=30)
    loop._throttled = True
    ticks = {"n": 0}

    async def fast_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 3:
            loop._stop.set()

    loop._grid_sleep = fast_sleep
    await loop._grid_loop(_grid_ctx())
    assert store.uploads == 0, "no room shots while memory is tight"
    assert loop._grid_cover_seconds == 0.0


def test_grid_holds_under_pressure():
    asyncio.run(_grid_holds_under_pressure())
    print("  grid proctor holds under memory pressure OK")


async def _grid_off_for_lookout():
    client = FakeMeetingClient(
        participants=[Participant("1", "A", video_on=True)], self_id="9")
    loop = CaptureLoop(client, FakeBackend(), NullStorage(), interval_seconds=300,
                       store_images=True, room_snapshot_seconds=30, lookout=True)
    assert loop.room_snapshot_seconds == 0, "a lookout renders nothing to photograph"


def test_grid_off_for_lookout():
    asyncio.run(_grid_off_for_lookout())
    print("  grid proctor off for a lookout OK")


def test_camera_picture_room_limit(monkeypatch):
    """The picture costs the browser about 100 MB in a full class, so the
    page is told the room size above which it stays off: 10 by default,
    tunable without a rebuild, 0 for no limit."""
    from bot.meeting_client import _camera_face_max_people
    monkeypatch.delenv("BOT_CAMERA_FACE_MAX_PEOPLE", raising=False)
    assert _camera_face_max_people() == 10
    monkeypatch.setenv("BOT_CAMERA_FACE_MAX_PEOPLE", "25")
    assert _camera_face_max_people() == 25
    monkeypatch.setenv("BOT_CAMERA_FACE_MAX_PEOPLE", "0")
    assert _camera_face_max_people() == 0
    monkeypatch.setenv("BOT_CAMERA_FACE_MAX_PEOPLE", "nonsense")
    assert _camera_face_max_people() == 10
    page = open("bot/static/zoom_client.js", encoding="utf-8").read()
    assert "cameraFaceMaxPeople" in page and "the picture stays off above" in page
