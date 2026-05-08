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


def _disk_free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return -1


def _human(n: int) -> str:
    if n < 0:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


def _safe_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"[UPLOAD] Could not delete {path}: {e}")


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

    original_path = None  # set after we know temp_dir; deleted post-trim
    try:
        store.update_job(
            job_id,
            status="downloading",
            progress=0.05,
            message="Downloading video...",
        )

        temp_dir = tempfile.mkdtemp(prefix=f"upload_{job_id}_")
        original_path = os.path.join(temp_dir, "recording.mp4")
        video_path = original_path

        # Pre-flight: peek at the file size and check we have room for it.
        # Free-tier Render ephemeral disk is small; if the recording can't
        # fit, fail fast with a clear message instead of hanging.
        head_resp = requests.head(req["video_url"], allow_redirects=True, timeout=30)
        announced_size = int(head_resp.headers.get("content-length", 0))
        free = _disk_free_bytes(temp_dir)
        logger.info(
            f"[UPLOAD] Job {job_id}: file ~{_human(announced_size)}, "
            f"disk free ~{_human(free)} at {temp_dir}"
        )
        # We need room for the original AND the trimmed copy briefly. We
        # delete the original right after trim, but during trim both exist.
        # Be generous: require 2.2x the file size to be safe.
        if announced_size and free > 0 and free < int(announced_size * 2.2):
            raise Exception(
                f"Not enough disk space: file is {_human(announced_size)} "
                f"but only {_human(free)} free. Free-tier Render disks are "
                f"small; try the Speaker View (smaller) or upgrade the plan."
            )

        logger.info(f"[UPLOAD] Downloading video for job {job_id}")
        # Long timeout: large recordings take a while; allow up to 1h.
        response = requests.get(req["video_url"], stream=True, timeout=3600)
        response.raise_for_status()

        total_size = int(response.headers.get("content-length", 0)) or announced_size
        downloaded = 0
        last_reported = 0.0
        last_logged = 0.0

        with open(original_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = downloaded / total_size
                    progress = 0.05 + (pct * 0.25)
                    if progress - last_reported >= 0.01:
                        store.update_job(job_id, progress=progress)
                        last_reported = progress
                    if pct - last_logged >= 0.10:
                        logger.info(
                            f"[UPLOAD] Job {job_id}: downloaded "
                            f"{_human(downloaded)} / {_human(total_size)} ({pct*100:.0f}%)"
                        )
                        last_logged = pct

        logger.info(
            f"[UPLOAD] Job {job_id}: download complete, "
            f"size on disk = {_human(os.path.getsize(original_path))}"
        )
        store.update_job(
            job_id,
            progress=0.30,
            message="Download complete. Processing...",
        )

        trimmer = VideoTrimmerService(output_dir=temp_dir)
        duration = trimmer.get_video_duration(original_path)
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
                original_path, start_time, end_time, progress_callback=trim_progress
            )
            if not trimmed_path:
                raise Exception("Video trimming failed")
            video_path = trimmed_path
            # KEY: remove the original immediately so we don't hold ~2x the
            # file size on disk while uploading. On free-tier disks this is
            # often the difference between "works" and "silently hangs".
            _safe_remove(original_path)
            logger.info(
                f"[UPLOAD] Job {job_id}: trimmed file = "
                f"{_human(os.path.getsize(video_path))}; original deleted; "
                f"disk free now {_human(_disk_free_bytes(temp_dir))}"
            )

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
