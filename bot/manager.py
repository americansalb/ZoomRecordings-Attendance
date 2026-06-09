"""
BotManager: the orchestration layer behind the TUTOR_BOT.md HTTP contract.

Tracks one meeting client per runtime_id, wires inbound chat to the backend
webhook, and (when capture is enabled) runs a per-student capture loop. Meeting
client + storage are injected via factories so this is testable with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .capture import CaptureContext, CaptureLoop
from .meeting_client import MeetingClient
from .signature import meeting_sdk_signature

logger = logging.getLogger(__name__)


@dataclass
class BotSession:
    runtime_id: str
    meeting_id: str
    session_ref: str
    display_name: str
    client: MeetingClient
    loop: Optional[CaptureLoop] = None
    task: Optional[asyncio.Task] = field(default=None)


def _passcode_from_join_url(join_url: Optional[str]) -> str:
    if not join_url:
        return ""
    try:
        qs = parse_qs(urlparse(join_url).query)
        return (qs.get("pwd") or [""])[0]
    except Exception:
        return ""


class BotManager:
    def __init__(
        self,
        config,
        backend,
        *,
        client_factory: Callable[..., MeetingClient],
        storage_factory: Callable[[bool, Optional[str]], Any],
    ):
        self.config = config
        self.backend = backend
        self.client_factory = client_factory
        self.storage_factory = storage_factory
        self._sessions: Dict[str, BotSession] = {}

    async def join(self, payload: Dict[str, Any]) -> str:
        meeting_id = str(payload.get("meeting_id") or "").strip()
        if not meeting_id:
            raise ValueError("meeting_id is required")
        if not (self.config.sdk_key and self.config.sdk_secret):
            raise RuntimeError("ZOOM_MEETING_SDK_KEY/ZOOM_MEETING_SDK_SECRET not configured")

        session_ref = str(payload.get("session_ref") or "")
        display_name = payload.get("display_name") or "AALB Assistant"
        runtime_id = f"bot_{uuid.uuid4().hex[:16]}"

        client = self.client_factory(page_url=self._page_url(), headless=self.config.headless)

        async def _on_chat(raw: Dict[str, Any]) -> None:
            sender = raw.get("sender") or {}
            await self.backend.post_event({
                "type": "chat",
                "session_ref": session_ref,
                "runtime_id": runtime_id,
                "channel": "dm" if raw.get("isPrivate") else "public",
                "participant_id": str(sender.get("userId") or ""),
                "participant_name": sender.get("name") or "",
                "text": raw.get("message") or raw.get("text") or "",
            })

        client.on_chat = _on_chat

        signature = meeting_sdk_signature(
            self.config.sdk_key, self.config.sdk_secret, meeting_id, role=0
        )
        await client.join(
            meeting_number=meeting_id,
            passcode=_passcode_from_join_url(payload.get("join_url")),
            display_name=display_name,
            signature=signature,
            sdk_key=self.config.sdk_key,
        )

        session = BotSession(runtime_id, meeting_id, session_ref, display_name, client)
        self._sessions[runtime_id] = session

        if payload.get("announce") and payload.get("announcement"):
            try:
                await client.send_chat(payload["announcement"])
            except Exception as e:
                logger.warning("announcement failed: %s", e)

        capture = payload.get("capture") or {}
        if capture.get("enabled"):
            storage = self.storage_factory(
                bool(capture.get("store_images", True)), self.config.drive_folder_id
            )
            loop = CaptureLoop(
                client, self.backend, storage,
                interval_seconds=int(capture.get("interval_seconds", 300)),
                store_images=bool(capture.get("store_images", True)),
            )
            ctx = CaptureContext(
                runtime_id=runtime_id,
                session_ref=session_ref,
                meeting_id=meeting_id,
                session_label=session_ref or meeting_id,
                bot_name=display_name,
            )
            session.loop = loop
            session.task = asyncio.create_task(loop.run(ctx))

        logger.info("[BOT] joined meeting %s as %s (capture=%s)",
                    meeting_id, runtime_id, bool(capture.get("enabled")))
        return runtime_id

    async def leave(self, runtime_id: str) -> None:
        session = self._sessions.pop(runtime_id, None)
        if not session:
            return
        if session.loop:
            session.loop.stop()
        if session.task:
            try:
                await asyncio.wait_for(session.task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                session.task.cancel()
        try:
            await session.client.leave()
        except Exception as e:
            logger.warning("leave error: %s", e)

    async def send(self, runtime_id: str, channel: str, text: str,
                   to_participant_id: Optional[str] = None) -> None:
        session = self._sessions.get(runtime_id)
        if not session:
            raise KeyError(f"unknown runtime_id {runtime_id}")
        to = to_participant_id if channel == "dm" else None
        await session.client.send_chat(text, to_user_id=to)

    def _page_url(self) -> str:
        return f"{self.config.public_base_url}/static/zoom_client.html"

    async def shutdown(self) -> None:
        for rid in list(self._sessions):
            await self.leave(rid)
