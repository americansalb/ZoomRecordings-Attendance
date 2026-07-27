"""
Client for talking back to the Phase 1 backend.

Posts inbound chat + lifecycle events to /api/tutor/bot/events, attendance rows
to /api/tutor/bot/attendance, and screenshot manifest rows to
/api/tutor/bot/screenshots, authenticated with the shared secret when
configured.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class BackendClient:
    def __init__(self, base_url: str, shared_secret: Optional[str] = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.shared_secret = shared_secret
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.shared_secret:
            h["X-Tutor-Bot-Secret"] = self.shared_secret
        return h

    async def _post(self, path: str, body: Dict[str, Any]) -> None:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=body, headers=self._headers())
                resp.raise_for_status()
        except httpx.HTTPError as e:
            # Never let a backend hiccup crash the meeting bot.
            logger.warning("backend POST %s failed: %s", path, e)

    async def post_event(self, event: Dict[str, Any]) -> None:
        await self._post("/api/tutor/bot/events", event)

    async def post_attendance(self, row: Dict[str, Any]) -> None:
        await self._post("/api/tutor/bot/attendance", row)

    async def post_screenshot(self, row: Dict[str, Any]) -> None:
        await self._post("/api/tutor/bot/screenshots", row)
