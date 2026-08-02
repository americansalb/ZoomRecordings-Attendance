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


class FakeDriveStorage(Storage):
    @property
    def stores_images(self):
        return True

    async def upload(self, *, data, filename, session_folder):
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
    assert len(backend.shots) == 2
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
    assert m2["Sam Lee"]["stored"] is False, "camera-off student isn't uploaded"
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
    # More cameras than one sweep's cap: the gallery steps each sweep so
    # every page of tiles gets its turn at a constant decode cost.
    assert client.advances == 2, "gallery advances once per sweep when over the cap"


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
