"""The breakout room test endpoint reaches the meeting client and returns
what it found, and the page defines the call it runs."""
import os

from fastapi.testclient import TestClient

from bot.app import build_app
from bot.config import Config
from bot.meeting_client import FakeMeetingClient
from bot.storage import NullStorage
from bot.tests.test_bot import FakeBackend


def test_breakout_test_endpoint_runs_the_client_test():
    seen = {}

    class Client(FakeMeetingClient):
        async def breakout_test(self, names=None, open_seconds=15, make_cohost=None):
            seen["names"] = names
            seen["open_seconds"] = open_seconds
            if make_cohost:
                seen["make_cohost"] = make_cohost
            return {"role": {"isHost": True}, "steps": [], "done": "finished"}

    cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                 sdk_key="KEY", sdk_secret="SECRET",
                 public_base_url="http://bot", headless=True, drive_folder_id=None)
    app = build_app(cfg, backend=FakeBackend(),
                    client_factory=lambda page_url, headless: Client(),
                    storage_factory=lambda s, f: NullStorage())
    c = TestClient(app)
    r = c.post("/bots", json={"meeting_id": "1", "session_ref": "breakouttest",
                              "display_name": "B"})
    assert r.status_code == 200, r.text
    rid = r.json()["runtime_id"]
    r = c.post(f"/bots/{rid}/breakout-test", json={"names": ["A", "B"], "open_seconds": 9})
    assert r.status_code == 200, r.text
    assert r.json()["done"] == "finished"
    assert seen == {"names": ["A", "B"], "open_seconds": 9}
    r = c.post(f"/bots/{rid}/breakout-test", json={"make_cohost": "Attendance Bot"})
    assert r.status_code == 200 and seen["make_cohost"] == "Attendance Bot"
    assert c.post("/bots/nope/breakout-test", json={}).status_code == 404


def test_page_defines_the_breakout_test():
    here = os.path.dirname(__file__)
    with open(os.path.join(here, "..", "static", "zoom_client.js")) as f:
        js = f.read()
    assert "window.zoomBreakoutTest = async" in js
    assert "client.openBreakoutRooms(" in js and "client.closeAllBreakoutRooms()" in js
