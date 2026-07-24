"""
Publish API — the queue, the plan, the settings, and the job.

Deliberately separate from routes/video_upload.py: the old Trim & Upload flow
keeps working untouched while this one is proven in real use.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from services import class_config
from services.class_config import ClassSettings, PublishConfig, VIEW_TYPES
from services.classroom_service import classroom_service
from services.job_store import get_job_store, using_redis
from services.publish_planner import plan_recording
from services.publish_worker import run_publish_job
from services.zoom_service import zoom_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/publish", tags=["Publish"])


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class OutputSpec(BaseModel):
    key: str
    folder: str
    download_url: str
    filename: Optional[str] = None
    drive_folders: List[str] = []


class PublishRequest(BaseModel):
    recording_id: str
    # Optional on purpose: a recording with no matched class still uploads to
    # Drive (into Unsorted/), it just doesn't get posted to Classroom.
    session_code: str = ""
    day_number: Optional[int] = None
    date_key: str
    title: str
    description: str = ""
    outputs: List[OutputSpec]
    start_seconds: float = 0
    end_seconds: Optional[float] = None
    course_id: str = ""
    topic_id: str = ""
    post_state: str = "PUBLISHED"
    share_mode: str = "VIEW"
    scheduled_time: Optional[str] = None


class ClassPayload(BaseModel):
    code: str
    label: str = ""
    color: str = "teal"
    timezone: str = "America/New_York"
    scheduled_start: str = ""
    scheduled_end: str = ""
    meeting_weekdays: List[int] = []
    first_class_date: str = ""
    pad_before_minutes: int = 5
    pad_after_minutes: int = 10
    views: List[str] = ["speaker"]
    filename_pattern: str = class_config.DEFAULT_FILENAME_PATTERN
    title_pattern: str = class_config.DEFAULT_TITLE_PATTERN
    drive_folder_id: str = ""
    classroom_course_id: str = ""
    classroom_course_name: str = ""
    classroom_topic_id: str = ""
    classroom_topic_name: str = ""
    post_state: str = "PUBLISHED"
    share_mode: str = "VIEW"


class SettingsPayload(BaseModel):
    classroom_subject: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    default_timezone: str = "America/New_York"


# ---------------------------------------------------------------------------
# the queue
# ---------------------------------------------------------------------------

@router.get("/queue")
async def publish_queue(
    days: int = Query(14, ge=1, le=90, description="How far back to look"),
    user_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """
    Every recent Zoom recording, already resolved into a publish plan.

    This is the whole screen in one call — no per-row follow-ups.
    """
    from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.now().strftime("%Y-%m-%d")

    try:
        if user_id:
            data = await zoom_service.list_recordings(user_id, from_date, to_date)
            recordings = data.get("meetings", [])
        else:
            recordings = await zoom_service.list_all_recordings(from_date, to_date)
    except Exception as e:                          # noqa: BLE001
        logger.error(f"[PUBLISH] Could not list recordings: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not reach Zoom: {e}")

    config = class_config.load()

    # Anything already published shows as published rather than as work to do.
    published = _published_index()

    plans: List[Dict[str, Any]] = []
    for recording in recordings:
        normalized = {
            "id": recording.get("uuid"),
            "meeting_id": recording.get("id"),
            "topic": recording.get("topic", ""),
            "start_time": recording.get("start_time"),
            "duration": recording.get("duration"),
            "host_name": recording.get("host_name", ""),
            "recording_files": [
                {
                    "id": f.get("id"),
                    "file_type": f.get("file_type"),
                    "file_size": f.get("file_size"),
                    "download_url": f.get("download_url"),
                    "recording_type": f.get("recording_type"),
                }
                for f in recording.get("recording_files", [])
            ],
        }
        plan = plan_recording(normalized, config)
        record = published.get(plan["recording_id"])
        plan["published"] = record
        plan["state"] = (
            "published" if record else ("ready" if plan["ready"] else "needs_attention")
        )
        plans.append(plan)

    plans.sort(key=lambda p: p.get("start_time") or "", reverse=True)

    return {
        "recordings": plans,
        "counts": {
            "ready": sum(1 for p in plans if p["state"] == "ready"),
            "needs_attention": sum(1 for p in plans if p["state"] == "needs_attention"),
            "published": sum(1 for p in plans if p["state"] == "published"),
        },
        "classroom_configured": bool(config.classroom_subject),
        "classes_configured": len(config.classes),
    }


def _published_index() -> Dict[str, Dict[str, Any]]:
    """recording_id -> summary, from completed publish jobs."""
    index: Dict[str, Dict[str, Any]] = {}
    try:
        for job in get_job_store().list_jobs(limit=500):
            if job.get("status") != "completed":
                continue
            req = job.get("request_data") or {}
            rec_id = req.get("recording_id")
            if not rec_id or rec_id in index:
                continue
            result = job.get("result") or {}
            index[rec_id] = {
                "job_id": job["job_id"],
                "published_at": result.get("published_at"),
                "files": result.get("files", []),
                "classroom": result.get("classroom"),
            }
    except Exception as e:                          # noqa: BLE001
        logger.warning(f"[PUBLISH] Could not read published history: {e}")
    return index


@router.post("/plan")
async def replan(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recompute a plan with the user's overrides applied.

    Used when someone assigns an unmatched recording to a class, or corrects
    the day number — the filename, title and trim all follow from that.
    """
    recording = payload.get("recording")
    if not recording:
        raise HTTPException(status_code=400, detail="Missing 'recording'")
    return plan_recording(
        recording,
        class_config.load(),
        day_override=payload.get("day_number"),
        session_override=payload.get("session_code"),
        manual_start=payload.get("manual_start"),
        manual_duration_minutes=payload.get("manual_duration_minutes"),
    )


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------

@router.post("/start")
async def start_publish(
    request: PublishRequest, background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """Queue a publish job and return immediately."""
    if not request.outputs:
        raise HTTPException(status_code=400, detail="Select at least one video to publish.")

    safe = re.sub(r"[^a-zA-Z0-9]", "", request.recording_id)[:20]
    job_id = f"publish_{safe}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    store = get_job_store()
    store.create_job(job_id, request.dict())

    if using_redis():
        from redis import Redis
        from rq import Queue

        queue = Queue(
            os.getenv("RQ_QUEUE", "uploads"),
            connection=Redis.from_url(os.getenv("REDIS_URL")),
        )
        queue.enqueue(
            run_publish_job,
            job_id,
            job_id=job_id,
            job_timeout=int(os.getenv("RQ_JOB_TIMEOUT", str(60 * 60 * 2))),
            result_ttl=60 * 60 * 24,
            failure_ttl=60 * 60 * 24,
        )
        logger.info(f"[PUBLISH] Enqueued {job_id} on RQ")
    else:
        background_tasks.add_task(run_publish_job, job_id)
        logger.info(f"[PUBLISH] Scheduled {job_id} as a BackgroundTask")

    return {"success": True, "job_id": job_id}


@router.get("/status/{job_id}")
async def publish_status(job_id: str) -> Dict[str, Any]:
    job = get_job_store().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": float(job.get("progress") or 0),
        "message": job.get("message") or "",
        "result": job.get("result"),
        "error": job.get("error"),
    }


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

@router.get("/settings")
async def get_settings() -> Dict[str, Any]:
    config = class_config.load()
    return {
        "classes": [c.to_dict() for c in config.classes.values()],
        "classroom_subject": config.classroom_subject,
        "webhook_url": config.webhook_url,
        "webhook_secret_set": bool(config.webhook_secret),
        "default_timezone": config.default_timezone,
        "view_types": VIEW_TYPES,
        "palette": class_config.PALETTE,
        "storage": class_config.storage_status(),
    }


@router.put("/settings")
async def put_settings(payload: SettingsPayload) -> Dict[str, Any]:
    config = class_config.load()
    config.classroom_subject = payload.classroom_subject.strip()
    config.webhook_url = payload.webhook_url.strip()
    if payload.default_timezone:
        config.default_timezone = payload.default_timezone.strip()
    if payload.webhook_secret:
        config.webhook_secret = payload.webhook_secret
    class_config.save(config)
    return {"success": True}


@router.put("/classes/{code}")
async def put_class(code: str, payload: ClassPayload) -> Dict[str, Any]:
    if code != payload.code:
        raise HTTPException(status_code=400, detail="Class code mismatch")
    settings = ClassSettings.from_dict(payload.dict())
    class_config.upsert_class(settings)
    return {"success": True, "class": settings.to_dict()}


@router.delete("/classes/{code}")
async def remove_class(code: str) -> Dict[str, Any]:
    if not class_config.delete_class(code):
        raise HTTPException(status_code=404, detail=f"No settings for class {code}")
    return {"success": True}


# ---------------------------------------------------------------------------
# Classroom lookups (for the settings dropdowns)
# ---------------------------------------------------------------------------

@router.get("/classroom/courses")
async def classroom_courses() -> Dict[str, Any]:
    """Courses the configured teacher teaches. Degrades to a reason, not a 500."""
    return classroom_service.list_courses(class_config.load().classroom_subject)


@router.get("/classroom/topics/{course_id}")
async def classroom_topics(course_id: str) -> Dict[str, Any]:
    return classroom_service.list_topics(class_config.load().classroom_subject, course_id)
