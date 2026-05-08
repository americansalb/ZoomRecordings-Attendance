"""
Video Upload API Routes

Endpoints for trimming and uploading Zoom recordings to Google Drive.

Job state lives in `services.job_store` (SQLite or Redis depending on
REDIS_URL). The actual download/trim/upload work lives in
`services.upload_worker.run_upload_job` so it can be invoked via FastAPI
BackgroundTasks (single-process mode) or RQ (with a separate worker).
"""

from datetime import datetime
import logging
import os
import re
import tempfile

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, List
import requests

from services.drive_service import drive_service
from services.job_store import get_job_store, using_redis
from services.upload_worker import run_upload_job
from services.video_trimmer import VideoTrimmerService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["video-upload"])


def _enqueue(job_id: str, background_tasks: BackgroundTasks) -> None:
    """Enqueue the job. Uses RQ when Redis-backed, BackgroundTasks otherwise."""
    if using_redis():
        # Lazy import; rq is only required in Redis mode.
        from redis import Redis
        from rq import Queue

        redis_url = os.getenv("REDIS_URL")
        queue_name = os.getenv("RQ_QUEUE", "uploads")
        # Long timeout: large videos can legitimately take a long time.
        job_timeout = int(os.getenv("RQ_JOB_TIMEOUT", str(60 * 60 * 2)))  # 2h
        q = Queue(queue_name, connection=Redis.from_url(redis_url))
        q.enqueue(
            run_upload_job,
            job_id,
            job_id=job_id,
            job_timeout=job_timeout,
            result_ttl=60 * 60 * 24,  # keep RQ result 1 day
            failure_ttl=60 * 60 * 24,
        )
        logger.info(f"[UPLOAD] Enqueued {job_id} on RQ queue '{queue_name}'")
    else:
        background_tasks.add_task(run_upload_job, job_id)
        logger.info(f"[UPLOAD] Scheduled {job_id} as FastAPI BackgroundTask")


class VideoPreviewRequest(BaseModel):
    """Request to preview a video for trimming."""
    video_url: str
    meeting_id: str


class VideoPreviewResponse(BaseModel):
    """Response with video duration info."""
    duration_seconds: float
    duration_formatted: str
    width: Optional[int] = None
    height: Optional[int] = None
    size_bytes: Optional[int] = None


class TrimUploadRequest(BaseModel):
    """Request to trim and upload a video."""
    meeting_id: str
    recording_title: str
    session_code: str
    meeting_date: str  # Format: "Nov10" or "11/10"
    video_url: str
    view_type: str  # "gallery" or "speaker"
    start_time: Optional[float] = None  # Seconds from start
    end_time: Optional[float] = None  # Seconds from start
    day_number: Optional[int] = None  # Override auto-detected day


class AutoTrimRequest(BaseModel):
    """Request to auto-trim based on schedule."""
    session_code: str
    meeting_date: str
    video_duration_seconds: float


class AutoTrimResponse(BaseModel):
    """Response with auto-trim times."""
    start_time: float
    end_time: float
    scheduled_start: Optional[str] = None
    scheduled_end: Optional[str] = None
    message: str


class UploadJobStatus(BaseModel):
    """Status of an upload job."""
    job_id: str
    status: str  # "pending", "downloading", "trimming", "uploading", "completed", "failed"
    progress: float  # 0.0 to 1.0
    message: str
    result: Optional[Dict] = None
    error: Optional[str] = None


@router.post("/preview")
async def preview_video(request: VideoPreviewRequest) -> VideoPreviewResponse:
    """
    Preview a video to get its duration for trimming.

    Downloads enough of the video to extract metadata.
    """
    temp_dir = None
    try:
        logger.info(f"[UPLOAD] Previewing video for meeting {request.meeting_id}")

        temp_dir = tempfile.mkdtemp(prefix="video_preview_")
        temp_path = os.path.join(temp_dir, "preview.mp4")

        response = requests.get(request.video_url, stream=True, timeout=30)
        response.raise_for_status()

        # Download just enough to get metadata (first 10MB should be sufficient)
        max_bytes = 10 * 1024 * 1024
        downloaded = 0

        with open(temp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if downloaded >= max_bytes:
                    break

        trimmer = VideoTrimmerService(output_dir=temp_dir)
        info = trimmer.get_video_info(temp_path)

        if not info or info.get('duration', 0) == 0:
            total_size = int(response.headers.get('content-length', 0))
            if total_size > 0:
                # Fallback estimate
                estimated_minutes = total_size / (1024 * 1024)
                estimated_seconds = estimated_minutes * 60
                return VideoPreviewResponse(
                    duration_seconds=estimated_seconds,
                    duration_formatted=trimmer.format_time(estimated_seconds),
                    size_bytes=total_size,
                )
            raise HTTPException(status_code=400, detail="Could not determine video duration")

        return VideoPreviewResponse(
            duration_seconds=info['duration'],
            duration_formatted=trimmer.format_time(info['duration']),
            width=info.get('width'),
            height=info.get('height'),
            size_bytes=info.get('size_bytes'),
        )

    except requests.RequestException as e:
        logger.error(f"[UPLOAD] Error fetching video: {e}")
        raise HTTPException(status_code=400, detail=f"Error fetching video: {str(e)}")

    except Exception as e:
        logger.error(f"[UPLOAD] Error previewing video: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        if temp_dir and os.path.exists(temp_dir):
            try:
                import shutil
                shutil.rmtree(temp_dir)
            except Exception:
                pass


@router.post("/auto-trim")
async def get_auto_trim_times(request: AutoTrimRequest) -> AutoTrimResponse:
    """
    Calculate auto-trim times based on scheduled session time.
    """
    try:
        scheduled = drive_service.get_scheduled_time(
            request.session_code, request.meeting_date
        )
        if not scheduled:
            return AutoTrimResponse(
                start_time=0,
                end_time=request.video_duration_seconds,
                message="Could not find scheduled time. Using full video duration.",
            )

        start_time_str = scheduled.get('start_time', '')
        end_time_str = scheduled.get('end_time', '')

        buffer_before = 60  # 1 minute
        buffer_after = 300  # 5 minutes
        start_time = max(0, buffer_before)
        end_time = min(
            request.video_duration_seconds,
            request.video_duration_seconds + buffer_after,
        )

        return AutoTrimResponse(
            start_time=start_time,
            end_time=end_time,
            scheduled_start=start_time_str,
            scheduled_end=end_time_str,
            message=f"Auto-trim based on scheduled time: {start_time_str} - {end_time_str}",
        )

    except Exception as e:
        logger.error(f"[UPLOAD] Error calculating auto-trim: {e}")
        return AutoTrimResponse(
            start_time=0,
            end_time=request.video_duration_seconds,
            message=f"Error calculating auto-trim: {str(e)}",
        )


@router.post("/start")
async def start_trim_upload(
    request: TrimUploadRequest,
    background_tasks: BackgroundTasks,
) -> Dict:
    """
    Start trimming and uploading a video to Google Drive.

    Persists job state, then enqueues the work (RQ if Redis is configured,
    otherwise FastAPI BackgroundTasks). Returns the job_id immediately.
    """
    safe_meeting_id = re.sub(r"[^a-zA-Z0-9]", "", request.meeting_id)[:20]
    job_id = f"upload_{safe_meeting_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    store = get_job_store()
    store.create_job(job_id, request.dict())
    _enqueue(job_id, background_tasks)

    return {
        "success": True,
        "job_id": job_id,
        "message": "Upload job started",
    }


@router.get("/status/{job_id}")
async def get_upload_status(job_id: str) -> UploadJobStatus:
    """Get the status of an upload job."""
    job = get_job_store().get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return UploadJobStatus(
        job_id=job_id,
        status=job["status"],
        progress=float(job.get("progress") or 0),
        message=job.get("message") or "",
        result=job.get("result"),
        error=job.get("error"),
    )


@router.get("/jobs")
async def list_upload_jobs() -> Dict:
    """List recent upload jobs."""
    jobs_raw = get_job_store().list_jobs(limit=200)
    jobs: List[Dict] = []
    for j in jobs_raw:
        req = j.get("request_data") or {}
        jobs.append({
            "job_id": j["job_id"],
            "status": j["status"],
            "progress": float(j.get("progress") or 0),
            "message": j.get("message") or "",
            "session_code": req.get("session_code"),
            "view_type": req.get("view_type"),
            "meeting_date": req.get("meeting_date"),
        })
    return {"jobs": jobs, "total": len(jobs)}


@router.get("/day-number/{session_code}/{meeting_date:path}")
async def get_day_number(session_code: str, meeting_date: str) -> Dict:
    """Get the day number for a session and date."""
    day_number = drive_service.get_day_number(session_code, meeting_date)
    return {
        "session_code": session_code,
        "meeting_date": meeting_date,
        "day_number": day_number if day_number is not None else 0,
        "found": day_number is not None,
    }
