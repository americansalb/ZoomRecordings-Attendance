"""
Live Tutor API routes.

Admin control plane (settings, reminders, policies, summon/dismiss, the approval
queue, message log) plus the inbound webhook the self-hosted bot calls to report
chat and lifecycle events.

Mounted under /api (see main.py), so paths are /api/tutor/...
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel

from services.tutor.service import get_tutor_service, TutorServiceError
from services.tutor.store import get_tutor_store
from services.tutor.bot_runtime import get_bot_runtime
from services.tutor.policy_responder import get_policy_responder
from services.tutor import store as store_mod

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tutor", tags=["live-tutor"])


# ------------------------------------------------------------------- schemas


class SettingsPatch(BaseModel):
    capabilities: Optional[Dict[str, bool]] = None
    autonomy: Optional[str] = None
    guardrails: Optional[Dict[str, Any]] = None
    bot: Optional[Dict[str, Any]] = None
    capture: Optional[Dict[str, Any]] = None


class ReminderIn(BaseModel):
    label: str
    message: str
    enabled: bool = True


class ReminderPatch(BaseModel):
    label: Optional[str] = None
    message: Optional[str] = None
    enabled: Optional[bool] = None


class PolicyIn(BaseModel):
    title: str
    content: str
    enabled: bool = True


class PolicyPatch(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    enabled: Optional[bool] = None


class SummonIn(BaseModel):
    meeting_id: str
    meeting_uuid: Optional[str] = None
    topic: Optional[str] = None
    session_code: Optional[str] = None
    join_url: Optional[str] = None
    overrides: Optional[Dict[str, Any]] = None


class ReminderPostIn(BaseModel):
    reminder_id: Optional[int] = None
    text: Optional[str] = None


class ManualMessageIn(BaseModel):
    channel: str  # "public" | "dm"
    text: str
    target_id: Optional[str] = None
    target_name: Optional[str] = None


class AiDmIn(BaseModel):
    target_id: str
    target_name: Optional[str] = None
    instruction: str


class ApproveIn(BaseModel):
    final_text: Optional[str] = None


class SimulateInboundIn(BaseModel):
    channel: str = "public"
    text: str
    participant_id: Optional[str] = None
    participant_name: Optional[str] = None


class ScreenshotIn(BaseModel):
    session_ref: Optional[str] = None
    runtime_id: Optional[str] = None
    participant_id: Optional[str] = None
    participant_name: Optional[str] = None
    registrant_id: Optional[str] = None
    captured_at: Optional[float] = None
    video_on: bool = False
    face_present: bool = False
    stored: bool = False
    image_url: Optional[str] = None
    drive_file_id: Optional[str] = None


def _actor(x_admin_user: Optional[str]) -> str:
    return x_admin_user or "admin"


# -------------------------------------------------------------------- status


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    store = get_tutor_store()
    bot = get_bot_runtime()
    responder = get_policy_responder()
    return {
        "success": True,
        "bot_configured": bot.available,
        "responder_available": responder.available,
        "pending_approvals": store.count_pending_approvals(),
        "active_sessions": len(store.list_active_sessions()),
        "settings": store.get_settings(),
    }


# ------------------------------------------------------------------ settings


@router.get("/settings")
async def get_settings() -> Dict[str, Any]:
    return {"success": True, "settings": get_tutor_store().get_settings()}


@router.patch("/settings")
async def patch_settings(patch: SettingsPatch) -> Dict[str, Any]:
    body = {k: v for k, v in patch.model_dump().items() if v is not None}
    settings = get_tutor_store().update_settings(body)
    return {"success": True, "settings": settings}


# ----------------------------------------------------------------- reminders


@router.get("/reminders")
async def list_reminders() -> Dict[str, Any]:
    return {"success": True, "reminders": get_tutor_store().list_reminders()}


@router.post("/reminders")
async def create_reminder(body: ReminderIn) -> Dict[str, Any]:
    r = get_tutor_store().create_reminder(body.label, body.message, body.enabled)
    return {"success": True, "reminder": r}


@router.patch("/reminders/{reminder_id}")
async def update_reminder(reminder_id: int, body: ReminderPatch) -> Dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    r = get_tutor_store().update_reminder(reminder_id, **fields)
    if not r:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return {"success": True, "reminder": r}


@router.delete("/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int) -> Dict[str, Any]:
    get_tutor_store().delete_reminder(reminder_id)
    return {"success": True}


# ------------------------------------------------------------------ policies


@router.get("/policies")
async def list_policies() -> Dict[str, Any]:
    return {"success": True, "policies": get_tutor_store().list_policies()}


@router.post("/policies")
async def create_policy(body: PolicyIn) -> Dict[str, Any]:
    p = get_tutor_store().create_policy(body.title, body.content, body.enabled)
    return {"success": True, "policy": p}


@router.patch("/policies/{policy_id}")
async def update_policy(policy_id: int, body: PolicyPatch) -> Dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    p = get_tutor_store().update_policy(policy_id, **fields)
    if not p:
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"success": True, "policy": p}


@router.delete("/policies/{policy_id}")
async def delete_policy(policy_id: int) -> Dict[str, Any]:
    get_tutor_store().delete_policy(policy_id)
    return {"success": True}


# ------------------------------------------------------------------ sessions


@router.get("/sessions")
async def list_sessions() -> Dict[str, Any]:
    return {"success": True, "sessions": get_tutor_store().list_active_sessions()}


@router.post("/sessions/summon")
async def summon(body: SummonIn, x_admin_user: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    try:
        session = await get_tutor_service().summon(
            body.meeting_id, meeting_uuid=body.meeting_uuid, topic=body.topic,
            session_code=body.session_code, join_url=body.join_url,
            overrides=body.overrides, summoned_by=_actor(x_admin_user),
        )
        return {"success": True, "session": session}
    except TutorServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/dismiss")
async def dismiss(session_id: int, x_admin_user: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    try:
        session = await get_tutor_service().dismiss(session_id, by=_actor(x_admin_user))
        return {"success": True, "session": session}
    except TutorServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/reminder")
async def post_reminder(
    session_id: int, body: ReminderPostIn, x_admin_user: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    try:
        msg = await get_tutor_service().post_reminder(
            session_id, reminder_id=body.reminder_id, text=body.text, by=_actor(x_admin_user)
        )
        return {"success": True, "message": msg}
    except TutorServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/message")
async def send_manual(
    session_id: int, body: ManualMessageIn, x_admin_user: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    try:
        msg = await get_tutor_service().send_manual_message(
            session_id, channel=body.channel, text=body.text,
            target_id=body.target_id, target_name=body.target_name, by=_actor(x_admin_user),
        )
        return {"success": True, "message": msg}
    except TutorServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/ai-dm")
async def request_ai_dm(session_id: int, body: AiDmIn) -> Dict[str, Any]:
    try:
        approval = await get_tutor_service().request_ai_dm(
            session_id, target_id=body.target_id, target_name=body.target_name,
            instruction=body.instruction,
        )
        return {"success": True, "approval": approval}
    except TutorServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/sessions/{session_id}/simulate-inbound")
async def simulate_inbound(session_id: int, body: SimulateInboundIn) -> Dict[str, Any]:
    """Test helper: feed a fake inbound chat message through the pipeline.

    Useful for testing policies and the approval flow without a live meeting.
    Drafts (if any) land in the approval queue -- nothing is sent.
    """
    store = get_tutor_store()
    session = store.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    approval = await get_tutor_service().handle_inbound_chat(
        session, channel=body.channel, text=body.text,
        participant_id=body.participant_id, participant_name=body.participant_name,
    )
    return {"success": True, "drafted": approval is not None, "approval": approval}


# ----------------------------------------------------------------- approvals


@router.get("/approvals")
async def list_approvals(status: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
    return {"success": True, "approvals": get_tutor_store().list_approvals(status=status, limit=limit)}


@router.post("/approvals/{approval_id}/approve")
async def approve(
    approval_id: int, body: ApproveIn, x_admin_user: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    try:
        approval = await get_tutor_service().approve(
            approval_id, by=_actor(x_admin_user), final_text=body.final_text
        )
        return {"success": True, "approval": approval}
    except TutorServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: int, x_admin_user: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    try:
        approval = get_tutor_service().reject(approval_id, by=_actor(x_admin_user))
        return {"success": True, "approval": approval}
    except TutorServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ------------------------------------------------------------------ messages


@router.get("/messages")
async def list_messages(
    session_id: Optional[int] = None,
    meeting_id: Optional[str] = None,
    channel: Optional[str] = None,
    limit: int = 300,
) -> Dict[str, Any]:
    msgs = get_tutor_store().list_messages(
        session_id=session_id, meeting_id=meeting_id, channel=channel, limit=limit
    )
    return {"success": True, "messages": msgs}


# --------------------------------------------------------------- screenshots


@router.get("/screenshots")
async def list_screenshots(
    session_id: Optional[int] = None,
    meeting_id: Optional[str] = None,
    participant_id: Optional[str] = None,
    limit: int = 500,
) -> Dict[str, Any]:
    shots = get_tutor_store().list_screenshots(
        session_id=session_id, meeting_id=meeting_id, participant_id=participant_id, limit=limit
    )
    return {"success": True, "screenshots": shots}


@router.post("/bot/screenshots")
async def ingest_screenshot(
    body: ScreenshotIn,
    x_tutor_bot_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Manifest row from the bot: who/when, video-on, face-present, and (if
    stored) the Drive link. One row per per-student snapshot."""
    _check_bot_secret(x_tutor_bot_secret)
    import time as _time

    store = get_tutor_store()
    session = None
    if body.session_ref:
        try:
            session = store.get_session(int(body.session_ref))
        except (TypeError, ValueError):
            session = None
    if session is None and body.runtime_id:
        session = store.get_session_by_runtime(body.runtime_id)

    shot = store.add_screenshot(
        captured_at=body.captured_at or _time.time(),
        video_on=body.video_on,
        face_present=body.face_present,
        session_id=session["id"] if session else None,
        meeting_id=(session.get("meeting_id") if session else None),
        participant_id=body.participant_id,
        participant_name=body.participant_name,
        registrant_id=body.registrant_id,
        stored=body.stored,
        image_url=body.image_url,
        drive_file_id=body.drive_file_id,
    )
    return {"success": True, "screenshot": shot}


# --------------------------------------------------------------- bot webhook


def _check_bot_secret(provided: Optional[str]) -> None:
    expected = os.getenv("TUTOR_BOT_SHARED_SECRET")
    if expected and provided != expected:
        raise HTTPException(status_code=401, detail="Invalid bot secret")


@router.post("/bot/events")
async def bot_events(
    request: Request,
    x_tutor_bot_secret: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Inbound events from the self-hosted bot. See TUTOR_BOT.md for shapes."""
    _check_bot_secret(x_tutor_bot_secret)

    try:
        event = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    etype = event.get("type")
    store = get_tutor_store()
    service = get_tutor_service()

    # Resolve which session this event belongs to.
    session = None
    if event.get("session_ref"):
        try:
            session = store.get_session(int(event["session_ref"]))
        except (TypeError, ValueError):
            session = None
    if session is None and event.get("runtime_id"):
        session = store.get_session_by_runtime(str(event["runtime_id"]))

    if etype == "chat":
        if not session:
            raise HTTPException(status_code=404, detail="No session for this event")
        approval = await service.handle_inbound_chat(
            session,
            channel=event.get("channel", "public"),
            text=event.get("text", ""),
            participant_id=event.get("participant_id"),
            participant_name=event.get("participant_name"),
        )
        return {"success": True, "drafted": approval is not None}

    if etype in ("joined", "ready"):
        if session:
            store.update_session(session["id"], status=store_mod.SESSION_IN_MEETING,
                                 runtime_id=event.get("runtime_id") or session.get("runtime_id"))
        return {"success": True}

    if etype in ("left", "ended"):
        if session:
            store.update_session(session["id"], status=store_mod.SESSION_LEFT)
        return {"success": True}

    if etype == "error":
        if session:
            store.update_session(session["id"], status=store_mod.SESSION_ERROR,
                                 error=str(event.get("error", "unknown")))
        return {"success": True}

    # Unknown / lifecycle events we don't act on yet (e.g. participant_joined).
    logger.info("[TUTOR] Unhandled bot event type: %s", etype)
    return {"success": True, "ignored": True}
