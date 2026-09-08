"""
CIA auto-upload triggers.

POST /api/cia/sweep     — run one sweep (returns 202 and works in the
                          background; the response says if one is already
                          running). This is what the attendance bot pings
                          on a timer, and the poke itself wakes this API
                          when the free tier has put it to sleep.
GET  /api/cia/status    — what the last sweep did, for a human checking.

Both are gated by a secret. The bot uses the shared secret it already
holds (X-Tutor-Bot-Secret); anything else can use CIA_SWEEP_SECRET via
X-CIA-Sweep-Secret. With neither secret configured on the server, the
endpoints refuse to run rather than stand open.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from services import cia_sweep

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cia", tags=["CIA"])


def _check_secret(bot_secret: Optional[str], sweep_secret: Optional[str]) -> None:
    expected_bot = os.getenv("TUTOR_BOT_SHARED_SECRET")
    expected_sweep = os.getenv("CIA_SWEEP_SECRET")
    if not expected_bot and not expected_sweep:
        raise HTTPException(
            status_code=503,
            detail="No sweep secret is configured on the server "
                   "(TUTOR_BOT_SHARED_SECRET or CIA_SWEEP_SECRET).",
        )
    if expected_bot and bot_secret == expected_bot:
        return
    if expected_sweep and sweep_secret == expected_sweep:
        return
    raise HTTPException(status_code=403, detail="Wrong or missing sweep secret.")


@router.post("/sweep", status_code=202)
async def trigger_sweep(
    x_tutor_bot_secret: Optional[str] = Header(default=None),
    x_cia_sweep_secret: Optional[str] = Header(default=None),
):
    _check_secret(x_tutor_bot_secret, x_cia_sweep_secret)
    if os.getenv("CIA_SWEEP_DISABLED"):
        return {"disabled": True}
    if cia_sweep._lock.locked():
        return {"started": False, "already_running": True}
    # Fire and return: a sweep can spend minutes downloading a recording,
    # and the caller only needs to know the poke landed.
    asyncio.create_task(cia_sweep.run_cia_sweep())
    logger.info("[CIA] Sweep triggered")
    return {"started": True}


@router.get("/status")
async def sweep_status(
    x_tutor_bot_secret: Optional[str] = Header(default=None),
    x_cia_sweep_secret: Optional[str] = Header(default=None),
):
    _check_secret(x_tutor_bot_secret, x_cia_sweep_secret)
    return {
        "running": cia_sweep._lock.locked(),
        "intake_folder": cia_sweep.cia_folder_id(),
        "lookback_days": cia_sweep.lookback_days(),
        "last_sweep": cia_sweep.last_sweep or None,
    }
