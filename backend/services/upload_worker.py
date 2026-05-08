"""
The trim-and-upload work, extracted so it can be invoked either as a
FastAPI BackgroundTask (SQLite mode) or an RQ task (Redis mode).

The function takes only a job_id; the actual request payload is read
from the job store. Progress is reported back through the same store.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from typing import Any, Dict

import requests

from services.drive_service import drive_service
from services.job_store import get_job_store
from services.video_trimmer import VideoTrimmerService

logger = logging.getLogger(__name__)


def _format_date_for_filename(date_str: str) -> str:
    """Convert '11/10' -> 'Nov10' (passthrough if already that form)."""
    if re.match(r"^[A-Za-z]{3}\d{1,2}$", date_str):
        return date_str
    match = re.match(r"^(\d{1,2})/(\d{1,2})$", date_str)
    if match:
        month_num = int(match.group(1))
        day = match.group(2)
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        if 1 <= month_num <= 12:
            return f"{months[month_num - 1]}{day}"
    return date_str


def run_upload_job(job_id: str) -> None:
    """
    Execute the full download/trim/upload pipeline for a job.

    Reports progress and final status back to the job store. Never raises;
    failures are recorded as job status='failed' so the frontend sees them.
    """
    store = get_job_store()
    job = store.get_job(job_id)
    if not job:
        logger.error(f"[UPLOAD] Job {job_id} not found in store; aborting")
        return

    req: Dict[str, Any] = job["request_data"]
    temp_dir = None
    trimmer = None

    try:
        store.update_job(
            job_id,
            status="downloading",
            progress=0.05,
            message="Downloading video...",
        )

        temp_dir = tempfile.mkdtemp(prefix=f"upload_{job_id}_")
        video_path = os.path.join(temp_dir, "recording.mp4")

        logger.info(f"[UPLOAD] Downloading video for job {job_id}")
        # Long timeout: large recordings take a while; allow up to 1h.
        response = requests.get(req["video_url"], stream=True, timeout=3600)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0))
        downloaded = 0
        last_reported = 0.0

        with open(video_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = downloaded / total_size
                    progress = 0.05 + (pct * 0.25)
                    # Throttle DB/Redis writes to once per ~1% change
                    if progress - last_reported >= 0.01:
                        store.update_job(job_id, progress=progress)
                        last_reported = progress

        store.update_job(
            job_id,
            progress=0.30,
            message="Download complete. Processing...",
        )

        trimmer = VideoTrimmerService(output_dir=temp_dir)
        duration = trimmer.get_video_duration(video_path)
        if not duration:
            raise Exception("Could not determine video duration")

        start_time = req.get("start_time") if req.get("start_time") is not None else 0
        end_time = req.get("end_time") if req.get("end_time") is not None else duration
        start_time = max(0, start_time)
        end_time = min(duration, end_time)

        needs_trim = (start_time > 0) or (end_time < duration)

        if needs_trim:
            store.update_job(
                job_id,
                status="trimming",
                message=(
                    f"Trimming video "
                    f"({trimmer.format_time(start_time)} - {trimmer.format_time(end_time)})..."
                ),
            )

            def trim_progress(pct: float) -> None:
                store.update_job(job_id, progress=0.30 + (pct / 100 * 0.20))

            trimmed_path = trimmer.trim_video(
                video_path, start_time, end_time, progress_callback=trim_progress
            )
            if not trimmed_path:
                raise Exception("Video trimming failed")
            video_path = trimmed_path

        store.update_job(job_id, progress=0.50)

        # Day number: caller-provided override > schedule lookup > 0
        day_number = req.get("day_number")
        if day_number is None:
            day_number = drive_service.get_day_number(req["session_code"], req["meeting_date"])
            if day_number is None:
                day_number = 0
                logger.warning("[UPLOAD] Could not determine day number, using 0")

        store.update_job(
            job_id,
            status="uploading",
            message=f"Uploading to Google Drive (Day {day_number})...",
        )

        def upload_progress(uploaded: int, total: int) -> None:
            if total > 0:
                store.update_job(job_id, progress=0.50 + (uploaded / total * 0.45))

        meeting_date_formatted = _format_date_for_filename(req["meeting_date"])

        result = drive_service.upload_video(
            file_path=video_path,
            session_code=req["session_code"],
            day_number=day_number,
            meeting_date=meeting_date_formatted,
            view_type=req["view_type"],
            progress_callback=upload_progress,
        )
        if not result:
            raise Exception("Failed to upload to Google Drive")

        store.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message="Upload complete!",
            result={
                "file_id": result["file_id"],
                "file_name": result["name"],
                "web_view_link": result["web_view_link"],
                "session_code": req["session_code"],
                "day_number": day_number,
                "view_type": req["view_type"],
                "trimmed": needs_trim,
                "start_time": start_time if needs_trim else None,
                "end_time": end_time if needs_trim else None,
            },
        )
        logger.info(f"[UPLOAD] Job {job_id} completed successfully")

    except Exception as e:
        logger.error(f"[UPLOAD] Job {job_id} failed: {e}", exc_info=True)
        store.update_job(
            job_id,
            status="failed",
            error=str(e),
            message=f"Upload failed: {e}",
        )

    finally:
        if trimmer:
            try:
                trimmer.cleanup()
            except Exception:
                pass
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
