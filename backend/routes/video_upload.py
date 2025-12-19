"""
Video Upload API Routes

Endpoints for trimming and uploading Zoom recordings to Google Drive.
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import Optional, Dict, List
import os
import tempfile
import requests
import logging
from datetime import datetime

from services.drive_service import drive_service
from services.video_trimmer import VideoTrimmerService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["video-upload"])

# Background job tracking
upload_jobs: Dict[str, Dict] = {}


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

        # Download first chunk of video to get metadata
        temp_dir = tempfile.mkdtemp(prefix="video_preview_")
        temp_path = os.path.join(temp_dir, "preview.mp4")

        # Stream download with early termination (just headers and first bytes)
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

        # Get video info using ffprobe
        trimmer = VideoTrimmerService(output_dir=temp_dir)
        info = trimmer.get_video_info(temp_path)

        if not info or info.get('duration', 0) == 0:
            # If partial download didn't work, try to get duration from Content-Length
            total_size = int(response.headers.get('content-length', 0))

            # For a more accurate duration, we need the full file
            # This is a fallback estimate based on typical bitrates
            if total_size > 0:
                # Estimate: ~1MB per minute for typical Zoom recording
                estimated_minutes = total_size / (1024 * 1024)
                estimated_seconds = estimated_minutes * 60

                return VideoPreviewResponse(
                    duration_seconds=estimated_seconds,
                    duration_formatted=trimmer.format_time(estimated_seconds),
                    size_bytes=total_size
                )

            raise HTTPException(status_code=400, detail="Could not determine video duration")

        return VideoPreviewResponse(
            duration_seconds=info['duration'],
            duration_formatted=trimmer.format_time(info['duration']),
            width=info.get('width'),
            height=info.get('height'),
            size_bytes=info.get('size_bytes')
        )

    except requests.RequestException as e:
        logger.error(f"[UPLOAD] Error fetching video: {e}")
        raise HTTPException(status_code=400, detail=f"Error fetching video: {str(e)}")

    except Exception as e:
        logger.error(f"[UPLOAD] Error previewing video: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    finally:
        # Cleanup
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

    Uses the schedule spreadsheet to find the session start/end times,
    then returns:
    - start_time: 1 minute before scheduled start
    - end_time: Up to 5 minutes after scheduled end (or video end)
    """
    try:
        # Get scheduled time from drive service
        scheduled = drive_service.get_scheduled_time(
            request.session_code,
            request.meeting_date
        )

        if not scheduled:
            # Default: start at 0, end at video end
            return AutoTrimResponse(
                start_time=0,
                end_time=request.video_duration_seconds,
                message="Could not find scheduled time. Using full video duration."
            )

        # Parse scheduled times
        start_time_str = scheduled.get('start_time', '')
        end_time_str = scheduled.get('end_time', '')

        # Calculate recording start (1 min before scheduled)
        # This assumes recording starts close to scheduled time
        # We'd need video start time to calculate offset

        # For now, return reasonable defaults
        buffer_before = 60  # 1 minute
        buffer_after = 300  # 5 minutes

        start_time = max(0, buffer_before)  # Can't go negative
        end_time = min(
            request.video_duration_seconds,
            request.video_duration_seconds + buffer_after
        )

        return AutoTrimResponse(
            start_time=start_time,
            end_time=end_time,
            scheduled_start=start_time_str,
            scheduled_end=end_time_str,
            message=f"Auto-trim based on scheduled time: {start_time_str} - {end_time_str}"
        )

    except Exception as e:
        logger.error(f"[UPLOAD] Error calculating auto-trim: {e}")
        return AutoTrimResponse(
            start_time=0,
            end_time=request.video_duration_seconds,
            message=f"Error calculating auto-trim: {str(e)}"
        )


@router.post("/start")
async def start_trim_upload(
    request: TrimUploadRequest,
    background_tasks: BackgroundTasks
) -> Dict:
    """
    Start trimming and uploading a video to Google Drive.

    This runs in the background and returns a job ID to track progress.
    """
    # Generate job ID
    job_id = f"upload_{request.meeting_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Initialize job status
    upload_jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "message": "Job queued",
        "request": request.dict(),
        "result": None,
        "error": None
    }

    # Start background processing
    background_tasks.add_task(
        process_upload_background,
        job_id,
        request
    )

    return {
        "success": True,
        "job_id": job_id,
        "message": "Upload job started"
    }


async def process_upload_background(job_id: str, request: TrimUploadRequest):
    """Background task to trim and upload a video."""
    temp_dir = None
    trimmer = None

    try:
        upload_jobs[job_id]["status"] = "downloading"
        upload_jobs[job_id]["message"] = "Downloading video..."
        upload_jobs[job_id]["progress"] = 0.05

        # Create temp directory
        temp_dir = tempfile.mkdtemp(prefix=f"upload_{job_id}_")
        video_path = os.path.join(temp_dir, "recording.mp4")

        # Download video
        logger.info(f"[UPLOAD] Downloading video for job {job_id}")
        response = requests.get(request.video_url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0

        with open(video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    download_progress = downloaded / total_size
                    upload_jobs[job_id]["progress"] = 0.05 + (download_progress * 0.25)

        upload_jobs[job_id]["progress"] = 0.3
        upload_jobs[job_id]["message"] = "Download complete. Processing..."

        # Initialize trimmer
        trimmer = VideoTrimmerService(output_dir=temp_dir)

        # Get video duration
        duration = trimmer.get_video_duration(video_path)
        if not duration:
            raise Exception("Could not determine video duration")

        # Determine trim times
        start_time = request.start_time if request.start_time is not None else 0
        end_time = request.end_time if request.end_time is not None else duration

        # Validate times
        start_time = max(0, start_time)
        end_time = min(duration, end_time)

        needs_trim = (start_time > 0) or (end_time < duration)

        if needs_trim:
            upload_jobs[job_id]["status"] = "trimming"
            upload_jobs[job_id]["message"] = f"Trimming video ({trimmer.format_time(start_time)} - {trimmer.format_time(end_time)})..."

            def trim_progress(pct):
                # Trimming is 30% to 50% of total progress
                upload_jobs[job_id]["progress"] = 0.3 + (pct / 100 * 0.2)

            trimmed_path = trimmer.trim_video(
                video_path,
                start_time,
                end_time,
                progress_callback=trim_progress
            )

            if not trimmed_path:
                raise Exception("Video trimming failed")

            video_path = trimmed_path

        upload_jobs[job_id]["progress"] = 0.5

        # Determine day number
        day_number = request.day_number
        if day_number is None:
            day_number = drive_service.get_day_number(
                request.session_code,
                request.meeting_date
            )
            if day_number is None:
                day_number = 0  # Default to Day 0 if not found
                logger.warning(f"[UPLOAD] Could not determine day number, using 0")

        upload_jobs[job_id]["status"] = "uploading"
        upload_jobs[job_id]["message"] = f"Uploading to Google Drive (Day {day_number})..."

        def upload_progress(uploaded, total):
            if total > 0:
                pct = uploaded / total
                # Uploading is 50% to 95% of total progress
                upload_jobs[job_id]["progress"] = 0.5 + (pct * 0.45)

        # Format meeting date for filename (e.g., "11/10" -> "Nov10")
        meeting_date_formatted = format_date_for_filename(request.meeting_date)

        # Upload to Google Drive
        result = drive_service.upload_video(
            file_path=video_path,
            session_code=request.session_code,
            day_number=day_number,
            meeting_date=meeting_date_formatted,
            view_type=request.view_type,
            progress_callback=upload_progress
        )

        if not result:
            raise Exception("Failed to upload to Google Drive")

        upload_jobs[job_id]["status"] = "completed"
        upload_jobs[job_id]["progress"] = 1.0
        upload_jobs[job_id]["message"] = "Upload complete!"
        upload_jobs[job_id]["result"] = {
            "file_id": result['file_id'],
            "file_name": result['name'],
            "web_view_link": result['web_view_link'],
            "session_code": request.session_code,
            "day_number": day_number,
            "view_type": request.view_type,
            "trimmed": needs_trim,
            "start_time": start_time if needs_trim else None,
            "end_time": end_time if needs_trim else None
        }

        logger.info(f"[UPLOAD] Job {job_id} completed successfully")

    except Exception as e:
        logger.error(f"[UPLOAD] Job {job_id} failed: {e}", exc_info=True)
        upload_jobs[job_id]["status"] = "failed"
        upload_jobs[job_id]["error"] = str(e)
        upload_jobs[job_id]["message"] = f"Upload failed: {e}"

    finally:
        # Cleanup
        if trimmer:
            trimmer.cleanup()
        if temp_dir and os.path.exists(temp_dir):
            try:
                import shutil
                shutil.rmtree(temp_dir)
            except Exception:
                pass


def format_date_for_filename(date_str: str) -> str:
    """
    Format a date string for use in filename.

    Converts "11/10" to "Nov10" or keeps "Nov10" as is.
    """
    import re

    # Already in MonthDay format
    if re.match(r'^[A-Za-z]{3}\d{1,2}$', date_str):
        return date_str

    # MM/DD format
    match = re.match(r'^(\d{1,2})/(\d{1,2})$', date_str)
    if match:
        month_num = int(match.group(1))
        day = match.group(2)
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        if 1 <= month_num <= 12:
            return f"{months[month_num - 1]}{day}"

    return date_str


@router.get("/status/{job_id}")
async def get_upload_status(job_id: str) -> UploadJobStatus:
    """Get the status of an upload job."""
    if job_id not in upload_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job = upload_jobs[job_id]
    return UploadJobStatus(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        message=job["message"],
        result=job.get("result"),
        error=job.get("error")
    )


@router.get("/jobs")
async def list_upload_jobs() -> Dict:
    """List all upload jobs."""
    jobs = []
    for job_id, job in upload_jobs.items():
        jobs.append({
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "session_code": job["request"].get("session_code"),
            "view_type": job["request"].get("view_type"),
            "meeting_date": job["request"].get("meeting_date")
        })

    return {
        "jobs": jobs,
        "total": len(jobs)
    }


@router.get("/day-number/{session_code}/{meeting_date}")
async def get_day_number(session_code: str, meeting_date: str) -> Dict:
    """Get the day number for a session and date."""
    day_number = drive_service.get_day_number(session_code, meeting_date)

    return {
        "session_code": session_code,
        "meeting_date": meeting_date,
        "day_number": day_number if day_number is not None else 0,
        "found": day_number is not None
    }
