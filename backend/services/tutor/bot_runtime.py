"""
Bot runtime adapter.

The backend never talks to the Zoom Meeting SDK directly -- that lives in your
self-hosted bot process. This module defines the thin contract between the two
so the rest of the app is agnostic to how the bot is implemented.

Outbound (backend -> bot), HTTP/JSON:
    POST   {base}/bots            {meeting_id, meeting_uuid?, join_url?, display_name,
                                   announce, announcement?, session_ref}      -> {runtime_id}
    DELETE {base}/bots/{rid}                                                  -> 200
    POST   {base}/bots/{rid}/messages  {channel: "public"|"dm",
                                        text, to_participant_id? }            -> 200

Inbound (bot -> backend), HTTP/JSON, posted to the webhook route in
routes/live_tutor.py (POST /api/tutor/bot/events). See TUTOR_BOT.md for the
full event shapes. Requests are authenticated with a shared secret header
(X-Tutor-Bot-Secret) when TUTOR_BOT_SHARED_SECRET is set.

Two implementations:
  - SelfHostedBotRuntime: HTTP to your bot (TUTOR_BOT_BASE_URL).
  - NullBotRuntime: logs only. Used when no bot URL is configured so the admin
    UI, approval queue, and message log are fully exercisable without a live
    meeting. Sends "succeed" (and are logged) but go nowhere.
"""

from __future__ import annotations

import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class BotRuntimeError(RuntimeError):
    """Raised when the bot runtime cannot fulfil a request."""


@dataclass
class JoinRequest:
    meeting_id: str
    session_ref: str               # our tutor_sessions row id, as a string
    display_name: str
    meeting_uuid: Optional[str] = None
    join_url: Optional[str] = None
    announce: bool = True
    announcement: Optional[str] = None
    capture: Optional[dict] = None  # screenshot capture config (enabled/interval/store)


class BotRuntime(ABC):
    """Abstract meeting-bot control plane."""

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether a real bot backend is configured."""

    @abstractmethod
    async def join(self, req: JoinRequest) -> str:
        """Ask the bot to join a meeting. Returns the runtime id."""

    @abstractmethod
    async def leave(self, runtime_id: str) -> None:
        """Ask the bot to leave a meeting."""

    @abstractmethod
    async def send_message(
        self,
        runtime_id: str,
        channel: str,
        text: str,
        to_participant_id: Optional[str] = None,
    ) -> None:
        """Send a public chat message or a direct message."""


class SelfHostedBotRuntime(BotRuntime):
    """Talks to a self-hosted Meeting SDK bot over HTTP."""

    def __init__(self, base_url: str, shared_secret: Optional[str] = None, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.shared_secret = shared_secret
        self.timeout = timeout
        logger.info(f"[TUTOR] SelfHostedBotRuntime -> {self.base_url}")

    @property
    def available(self) -> bool:
        return True

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.shared_secret:
            h["X-Tutor-Bot-Secret"] = self.shared_secret
        return h

    async def join(self, req: JoinRequest) -> str:
        payload = {
            "meeting_id": req.meeting_id,
            "meeting_uuid": req.meeting_uuid,
            "join_url": req.join_url,
            "display_name": req.display_name,
            "announce": req.announce,
            "announcement": req.announcement,
            "session_ref": req.session_ref,
            "capture": req.capture,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/bots", json=payload, headers=self._headers()
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPError as e:
                raise BotRuntimeError(f"join failed: {e}") from e
        runtime_id = data.get("runtime_id") or data.get("id")
        if not runtime_id:
            raise BotRuntimeError("bot did not return a runtime_id")
        return str(runtime_id)

    async def leave(self, runtime_id: str) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.delete(
                    f"{self.base_url}/bots/{runtime_id}", headers=self._headers()
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise BotRuntimeError(f"leave failed: {e}") from e

    async def send_message(
        self,
        runtime_id: str,
        channel: str,
        text: str,
        to_participant_id: Optional[str] = None,
    ) -> None:
        payload = {"channel": channel, "text": text}
        if to_participant_id:
            payload["to_participant_id"] = to_participant_id
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    f"{self.base_url}/bots/{runtime_id}/messages",
                    json=payload,
                    headers=self._headers(),
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise BotRuntimeError(f"send failed: {e}") from e


class NullBotRuntime(BotRuntime):
    """No-op runtime: records intent in the log, sends nothing to a meeting."""

    @property
    def available(self) -> bool:
        return False

    async def join(self, req: JoinRequest) -> str:
        rid = f"null-{uuid.uuid4().hex[:12]}"
        logger.warning(
            "[TUTOR] No bot configured (TUTOR_BOT_BASE_URL unset). "
            "Simulating join of meeting %s as %s.", req.meeting_id, rid
        )
        return rid

    async def leave(self, runtime_id: str) -> None:
        logger.warning("[TUTOR] (null) leave %s", runtime_id)

    async def send_message(
        self,
        runtime_id: str,
        channel: str,
        text: str,
        to_participant_id: Optional[str] = None,
    ) -> None:
        logger.warning(
            "[TUTOR] (null) would send %s message via %s%s: %s",
            channel, runtime_id,
            f" -> {to_participant_id}" if to_participant_id else "",
            text[:120],
        )


_runtime: Optional[BotRuntime] = None


def get_bot_runtime() -> BotRuntime:
    """Self-hosted runtime if TUTOR_BOT_BASE_URL is set, else the null runtime."""
    global _runtime
    if _runtime is None:
        base_url = os.getenv("TUTOR_BOT_BASE_URL")
        if base_url:
            _runtime = SelfHostedBotRuntime(
                base_url, shared_secret=os.getenv("TUTOR_BOT_SHARED_SECRET")
            )
        else:
            _runtime = NullBotRuntime()
    return _runtime
