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
from bot.meeting_client import FakeMeetingClient, Participant
from bot.storage import NullStorage, Storage
from bot.app import build_app


class FakeBackend:
    def __init__(self):
        self.events = []
        self.shots = []

    async def post_event(self, e):
        self.events.append(e)

    async def post_screenshot(self, r):
        self.shots.append(r)


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
    client = FakeMeetingClient(participants=parts, frames={"1": b"FACEDATA"})
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
    assert maria["video_on"] is True and maria["face_present"] is True and maria["stored"] is False
    assert maria["participant_id"] == "1"           # attributed by Zoom id
    assert sam["video_on"] is False and sam["face_present"] is False
    assert len(backend.shots) == 2

    # store-to-Drive path
    backend2 = FakeBackend()
    loop2 = CaptureLoop(client, backend2, FakeDriveStorage(), interval_seconds=300,
                        store_images=True, face_detector=detector)
    rows2 = await loop2.run_once(ctx)
    m2 = {r["participant_name"]: r for r in rows2}
    assert m2["Maria Gomez"]["stored"] is True and m2["Maria Gomez"]["image_url"].startswith("https://drive/")
    assert m2["Sam Lee"]["stored"] is False, "camera-off student isn't uploaded"
    print("  capture pipeline OK")


def test_capture():
    asyncio.run(_capture_pipeline())


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

    # leave
    assert c.request("DELETE", f"/bots/{rid}").status_code == 200
    assert client0.joined is False
    print("  contract OK")


def run():
    test_signature()
    test_capture()
    test_contract()
    print("BOT TESTS PASSED")


if __name__ == "__main__":
    run()
