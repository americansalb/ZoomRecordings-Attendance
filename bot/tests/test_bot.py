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
from bot.manager import BotManager
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
    assert sam["video_on"] is False and sam["face_present"] is False
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
    assert att["Maria Gomez"]["face_present"] is False
    assert att["Maria Gomez"]["observed_seconds"] == 60
    assert all(s["stored"] is False for s in backend.shots)
    print("  attendance without frames OK")


async def _self_skip_by_id():
    """The bot is skipped by user id, not by display name."""
    parts = [Participant("1", "AALB Assistant", video_on=True),   # a real student
             Participant("99", "AALB Assistant", video_on=True)]  # the bot
    client = FakeMeetingClient(participants=parts, self_id="99")
    backend = FakeBackend()
    ctx = CaptureContext(runtime_id="r", session_ref="5", meeting_id="m",
                         session_label="5", bot_name="AALB Assistant")
    loop = CaptureLoop(client, backend, NullStorage(), interval_seconds=300,
                       store_images=False)
    rows = await loop.run_once(ctx)
    ids = {r["participant_id"] for r in rows}
    assert ids == {"1"}, f"a student sharing the bot's name must survive, got {ids}"
    print("  self-skip by id OK")


def test_capture():
    asyncio.run(_capture_pipeline())
    asyncio.run(_attendance_without_frames())
    asyncio.run(_self_skip_by_id())


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


def run():
    test_signature()
    test_capture()
    test_contract()
    test_meeting_end_is_reported()
    test_failed_join_cleans_up()
    test_join_passes_credentials()
    print("BOT TESTS PASSED")


if __name__ == "__main__":
    run()
