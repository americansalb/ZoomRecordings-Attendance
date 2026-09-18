"""
Putting an old recording onto an academy class.

GET  /api/academy/review    which recordings look like which class night
POST /api/academy/publish    send one of them, chosen by a person

THIS SERVER DOES NONE OF THE WORK. The academy holds the archive, the class
timetable and the posting, so it answers both of these and this is a relay. The
screen lives here because this is where recordings are looked at.

The relay exists for one reason: the shared secret. A browser must never carry
it, so the page calls this server and this server calls the academy.

Matching is on the session number in the Zoom title. A recording called
"Session 127. Mondays..." belongs to the academy class named for Session 127,
and the night is whichever one the recording's hours actually overlap. The
academy decides all of that; the answers are passed through as they come.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import requests
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/academy", tags=["Academy"])

# Long enough for a cut and an upload of a three hour class, which is what a
# publish actually does on the other end before it answers.
PUBLISH_TIMEOUT = 1800
REVIEW_TIMEOUT = 120


def _academy_base() -> str:
    return (os.getenv("ACADEMY_URL") or "https://academy.aalb.org").rstrip("/")


def _secret() -> str:
    return (
        os.getenv("CLASS_BOT_SHARED_SECRET")
        or os.getenv("TUTOR_BOT_SHARED_SECRET")
        or ""
    ).strip()


def is_configured() -> bool:
    return bool(_secret())


def _call(method: str, path: str, *, timeout: int, **kwargs) -> Dict[str, Any]:
    """
    One call to the academy, with its errors turned into ours.

    A failure here is nearly always one of three things and the message says
    which: no secret on this server, a secret the academy does not accept, or
    the academy not answering. Anything vaguer sends somebody reading logs on
    both sides for an hour.
    """
    if not is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "This server has no academy secret, so it cannot post recordings. "
                "Set CLASS_BOT_SHARED_SECRET to the same value the academy uses."
            ),
        )

    url = f"{_academy_base()}/api/class-recordings{path}"
    try:
        response = requests.request(
            method,
            url,
            headers={"X-Tutor-Bot-Secret": _secret()},
            timeout=timeout,
            **kwargs,
        )
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502, detail=f"Could not reach the academy: {e}"
        ) from e

    if response.status_code == 401:
        raise HTTPException(
            status_code=502,
            detail="The academy refused this server's secret. The two do not match.",
        )
    if response.status_code == 503:
        raise HTTPException(
            status_code=503,
            detail="The academy has no secret configured, so it is not accepting this.",
        )
    if not response.ok:
        detail = ""
        try:
            detail = response.json().get("error", "")
        except ValueError:
            detail = response.text[:300]
        raise HTTPException(
            status_code=response.status_code,
            detail=detail or f"The academy answered {response.status_code}.",
        )

    try:
        return response.json()
    except ValueError as e:
        raise HTTPException(
            status_code=502, detail="The academy sent something that is not JSON."
        ) from e


@router.get("/review")
async def review(
    limit: int = Query(200, ge=1, le=500),
    session: Optional[str] = Query(None, description="Only this session number"),
) -> Dict[str, Any]:
    """Every recording with the class night it looks like. Changes nothing."""
    params: Dict[str, Any] = {"limit": limit}
    if session:
        params["session"] = session
    data = _call("GET", "/review", timeout=REVIEW_TIMEOUT, params=params)
    data["academy_url"] = _academy_base()
    return data


class PublishToAcademy(BaseModel):
    zoom_file_id: str
    cohort_day_id: int


@router.post("/publish")
async def publish(request: PublishToAcademy) -> Dict[str, Any]:
    """
    Send one recording to one night.

    The night is whichever one was chosen on screen, not re-derived, so a match
    the academy got wrong can be corrected by hand. The cut, the refusal to
    write over a trainer's own link and the notification are the academy's, the
    same as when it posts one by itself.
    """
    return _call(
        "POST",
        "/publish",
        timeout=PUBLISH_TIMEOUT,
        json={
            "zoom_file_id": request.zoom_file_id,
            "cohort_day_id": request.cohort_day_id,
        },
    )


@router.get("/status")
async def status() -> Dict[str, Any]:
    """Whether this server can talk to the academy at all, for a person checking."""
    return {
        "academy_url": _academy_base(),
        "secret_configured": is_configured(),
    }
