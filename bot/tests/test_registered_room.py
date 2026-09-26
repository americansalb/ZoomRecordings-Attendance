"""A room that gives everyone their own link lets the bot in with its own.

Found 2026-09-26, the first class in Session 142's room: Zoom calls such a
room a registered meeting, and it refuses anybody who joins without the
registrant's token ("This meeting requires registration"). The control
plane registers the bot and hands over the bot's own link; the token rides
in it as ?tk= and must reach the Web SDK's join, with the email the bot
was registered under. Every other room joins exactly as before.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from bot.config import Config
from bot.manager import BotManager, _registrant_token_from_join_url
from bot.meeting_client import FakeMeetingClient, Participant
from bot.storage import NullStorage
from bot.tests.test_bot import FakeBackend


class RecordingClient(FakeMeetingClient):
    """A fake that keeps what it was asked to join with."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.join_kwargs = None

    async def join(self, **kwargs) -> None:
        self.join_kwargs = kwargs
        self.joined = True


def _join(payload):
    created = []

    def client_factory(page_url, headless):
        c = RecordingClient(participants=[Participant("1", "Maria")], self_id="99")
        created.append(c)
        return c

    cfg = Config(backend_url="http://backend", bot_shared_secret=None,
                 sdk_key="KEY", sdk_secret="SECRET",
                 public_base_url="http://bot", headless=True, drive_folder_id=None)
    manager = BotManager(cfg, FakeBackend(), client_factory=client_factory,
                         storage_factory=lambda s, f: NullStorage())

    async def scenario():
        rid = await manager.join(payload)
        kwargs = created[-1].join_kwargs
        await manager.leave(rid)
        return kwargs

    return asyncio.run(scenario())


def test_token_is_read_out_of_the_bots_own_link():
    url = "https://us06web.zoom.us/w/86101234567?tk=ABC-123.def&pwd=Zx9"
    assert _registrant_token_from_join_url(url) == "ABC-123.def"
    assert _registrant_token_from_join_url("https://zoom.us/j/86101234567?pwd=Zx9") == ""
    assert _registrant_token_from_join_url(None) == ""
    assert _registrant_token_from_join_url("not a link") == ""


def test_a_registered_room_joins_with_the_token_and_the_email():
    kw = _join({
        "meeting_id": "86101234567", "session_ref": "botsession:5",
        "display_name": "AALB Attendance Bot",
        "join_url": "https://us06web.zoom.us/w/86101234567?tk=TOKEN&pwd=Zx9",
        "user_email": "attendance@aalb.org",
    })
    assert kw["tk"] == "TOKEN"
    assert kw["user_email"] == "attendance@aalb.org"
    # The passcode still comes out of the same link.
    assert kw["passcode"] == "Zx9"


def test_a_token_sent_on_its_own_is_used():
    kw = _join({
        "meeting_id": "86101234567", "session_ref": "botsession:6",
        "join_url": "https://zoom.us/j/86101234567?pwd=Zx9",
        "tk": "SENT", "user_email": "attendance@aalb.org",
    })
    assert kw["tk"] == "SENT"


def test_every_other_room_joins_exactly_as_before():
    kw = _join({
        "meeting_id": "86101234567", "session_ref": "botsession:7",
        "join_url": "https://zoom.us/j/86101234567?pwd=Zx9",
        "user_email": "attendance@aalb.org",
    })
    assert "tk" not in kw
    # No token, no email: an ordinary room never sees either.
    assert "user_email" not in kw


def test_the_page_passes_the_token_to_zooms_join():
    page = (Path(__file__).resolve().parents[1] / "static" / "zoom_client.js").read_text()
    assert "if (cfg.tk) joinArgs.tk = cfg.tk;" in page
    assert "joinArgs.userEmail = cfg.userEmail" in page
    client = (Path(__file__).resolve().parents[1] / "meeting_client.py").read_text()
    assert '"tk": tk or None' in client
    assert '"userEmail": user_email or None' in client
