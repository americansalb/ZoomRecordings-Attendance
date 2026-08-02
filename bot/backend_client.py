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
        self._client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.shared_secret:
            h["X-Tutor-Bot-Secret"] = self.shared_secret
        return h

    def _http(self) -> httpx.AsyncClient:
        # One live connection, reused. A fresh client per post meant a full
        # TLS handshake every second at a one second observation pace,
        # which is pure churn and puts handshake latency inside the sweep.
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _post(self, path: str, body: Dict[str, Any]) -> None:
        url = f"{self.base_url}{path}"
        try:
            resp = await self._http().post(url, json=body, headers=self._headers())
            resp.raise_for_status()
        except httpx.HTTPError as e:
            # Never let a backend hiccup crash the meeting bot.
            logger.warning("backend POST %s failed: %s", path, e)

    async def post_event(self, event: Dict[str, Any]) -> None:
        await self._post("/api/tutor/bot/events", event)

    async def post_attendance(self, row: Dict[str, Any]) -> None:
        await self._post("/api/tutor/bot/attendance", row)

    async def post_attendance_batch(self, *, session_ref: str, runtime_id: str,
                                    captured_at: float, rows: list) -> None:
        """One request per sweep, not one per person.

        The ingest accepts {rows: [...]} and lands each row on its own
        timestamp. The old way, 25 people at a one second pace was 25
        sequential round trips per sweep, several seconds of pure wire
        time: the difference between a one second notebook and a lie.
        """
        await self._post("/api/tutor/bot/attendance", {
            "session_ref": session_ref,
            "runtime_id": runtime_id,
            "captured_at": captured_at,
            "rows": rows,
        })

    async def post_screenshot(self, row: Dict[str, Any]) -> None:
        await self._post("/api/tutor/bot/screenshots", row)
