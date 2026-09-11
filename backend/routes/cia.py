"""
CIA auto-upload triggers.

POST /api/cia/sweep     — run one sweep (returns 202 and works in the
                          background; the response says if one is already
                          running). This is what the attendance bot pings
                          on a timer, and the poke itself wakes this API
                          when the free tier has put it to sleep.
GET  /api/cia/status    — what the last sweep did, for a human checking.

THE SECRET RULE IS THE SAME AS EVERY OTHER BOT ENDPOINT (see
routes/live_tutor.py): when TUTOR_BOT_SHARED_SECRET (or CIA_SWEEP_SECRET)
is configured on this server the caller must send it; when neither is
configured the call is accepted. The first version refused with 503
instead, and the live deployment runs with no shared secret, so every
poke the bot made was refused and nothing was delivered while three exams
sat on Zoom (owner, 2026-09-08: "why are all these unmatched"). Triggering
a sweep is idempotent and delivers only into the wizard's own folder, so
an open trigger costs nothing. The status view is the one thing that
carries recording titles (candidate names): with no secret configured it
shows counts and error messages only.
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


def secret_configured() -> bool:
    return bool(os.getenv("TUTOR_BOT_SHARED_SECRET") or os.getenv("CIA_SWEEP_SECRET"))


def _check_secret(bot_secret: Optional[str], sweep_secret: Optional[str]) -> None:
    expected_bot = os.getenv("TUTOR_BOT_SHARED_SECRET")
    expected_sweep = os.getenv("CIA_SWEEP_SECRET")
    if not expected_bot and not expected_sweep:
        return
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
    return cia_sweep.status_view(full=secret_configured())
