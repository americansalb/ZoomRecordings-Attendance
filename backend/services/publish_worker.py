"""
The publish pipeline: trim -> Drive -> Classroom -> (optional) your webhook.

Runs as a FastAPI BackgroundTask or an RQ task, same as the existing upload
worker, and reports progress through the same job store.

Two deliberate differences from services/upload_worker.py:

1. It trims straight from the Zoom URL. ffmpeg reads over HTTP and seeks with
   range requests, so we never stage the full original on disk — only the
   trimmed result. The old path needed ~2.2x the file size free and failed
   loudly on small disks. Falls back to download-then-trim if that fails.

2. Drive is the only step that must succeed. Classroom failing (or not being
   set up at all) leaves you with an uploaded video and a link to post by
   hand, rather than losing the work.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from services import class_config
from services.classroom_service import classroom_service
from services.drive_service import DriveUploadError, drive_service
from services.job_store import get_job_store
from services.video_trimmer import VideoTrimmerService

logger = logging.getLogger(__name__)

# Progress is shared across however many videos a recording produces.
_TRIM_SHARE = 0.35
_UPLOAD_SHARE = 0.55
_CLASSROOM_SHARE = 0.10


def _human(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


def _folders_for(output: Dict[str, Any], req: Dict[str, Any]) -> List[str]:
    """
    Where in Drive this video goes.

    The path comes from the plan, so an unmatched recording lands in
    Unsorted/<view> under its Zoom title rather than being blocked on someone
    configuring a class first.
    """
    return output.get("drive_folders") or [
        f"Session {req['session_code']}" if req.get("session_code") else "Unsorted",
        output.get("folder") or "Speaker + Screenshare",
    ]


def _notify_webhook(url: str, secret: str, payload: Dict[str, Any]) -> None:
    """Best-effort POST to a downstream service. Never fails the job."""
    if not url:
        return
    try:
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Publish-Secret"] = secret
        requests.post(url, json=payload, headers=headers, timeout=15)
        logger.info(f"[PUBLISH] Notified webhook {url}")
    except requests.RequestException as e:
        logger.warning(f"[PUBLISH] Webhook {url} failed (ignored): {e}")


def run_publish_job(job_id: str) -> None:
    """
    Execute one publish job. Never raises — failures land in the job store so
    the UI can show them.
    """
    store = get_job_store()
    job = store.get_job(job_id)
    if not job:
        logger.error(f"[PUBLISH] Job {job_id} not found; aborting")
        return

    req: Dict[str, Any] = job["request_data"]
    outputs: List[Dict[str, Any]] = req.get("outputs") or []
    temp_dir: Optional[str] = None
    trimmer: Optional[VideoTrimmerService] = None
    uploaded: List[Dict[str, Any]] = []

    try:
        if not outputs:
            raise ValueError("Nothing to publish — no videos selected.")

        temp_dir = tempfile.mkdtemp(prefix=f"publish_{job_id}_")
        trimmer = VideoTrimmerService(output_dir=temp_dir)

        start = float(req.get("start_seconds") or 0)
        end = req.get("end_seconds")
        end = float(end) if end is not None else None

        per_video = 1.0 / len(outputs)

        # Prove we can write to Drive before spending twenty minutes trimming.
        # Two cheap calls per folder, and the result is cached for the upload.
        store.update_job(
            job_id, status="preparing", progress=0.0,
            message="Checking the Drive folder...",
        )
        for output in outputs:
            label = output.get("folder") or output.get("key") or "video"
            try:
                drive_service.ensure_path(_folders_for(output, req))
            except DriveUploadError as e:
                raise RuntimeError(f"{label} — {e}") from e

        for index, output in enumerate(outputs):
            base = index * per_video
            label = output.get("folder") or output.get("key") or "video"

            # ---- trim ----------------------------------------------------
            store.update_job(
                job_id,
                status="trimming",
                progress=base,
                message=f"Trimming {label} ({index + 1} of {len(outputs)})...",
            )

            def on_trim(pct: float, _base=base, _share=per_video) -> None:
                store.update_job(job_id, progress=_base + (pct / 100) * _share * _TRIM_SHARE)

            local_path = trimmer.trim_from_source(
                source=output["download_url"],
                start_time=start,
                end_time=end,
                output_filename=f"{index}_{output.get('key', 'video')}.mp4",
                progress_callback=on_trim,
            )
            if not local_path:
                raise RuntimeError(
                    f"Could not trim {label}. The recording may still be processing in Zoom."
                )

            # Free space is logged alongside the size because the two failures
            # that look alike from the UI — a trim that ran out of room and an
            # upload Google refused — are told apart by this line.
            free = shutil.disk_usage(temp_dir).free
            logger.info(
                f"[PUBLISH] {job_id}: trimmed {label} -> "
                f"{_human(os.path.getsize(local_path))} ({_human(free)} disk free)"
            )

            # ---- Drive ---------------------------------------------------
            store.update_job(
                job_id,
                status="uploading",
                progress=base + per_video * _TRIM_SHARE,
                message=f"Uploading {label} to Drive...",
            )

            def on_upload(done: int, total: int, _base=base, _share=per_video) -> None:
                if total > 0:
                    store.update_job(
                        job_id,
                        progress=_base + _share * (_TRIM_SHARE + (done / total) * _UPLOAD_SHARE),
                    )

            folders = _folders_for(output, req)
            try:
                result = drive_service.upload_to_path(
                    file_path=local_path,
                    folder_names=folders,
                    file_name=output.get("filename") or os.path.basename(local_path),
                    progress_callback=on_upload,
                )
            except DriveUploadError as e:
                # DriveUploadError already says what to do about it; keep the
                # view name on the front so it's clear which video stopped.
                raise RuntimeError(f"{label} — {e}") from e
            if not result:
                raise RuntimeError(f"Drive upload failed for {label}.")

            uploaded.append({
                "key": output.get("key"),
                "label": label,
                "file_id": result["file_id"],
                "name": result["name"],
                "link": result["web_view_link"],
            })

            # Free the trimmed file straight away; the next video needs the room.
            try:
                os.remove(local_path)
            except OSError:
                pass

        # ---- Classroom ---------------------------------------------------
        store.update_job(
            job_id,
            status="posting",
            progress=1.0 - _CLASSROOM_SHARE,
            message="Posting to Google Classroom...",
        )

        config = class_config.load()

        # Students reach the video through the "anyone with the link" permission
        # set at upload, so this is not what makes the post work. It gives the
        # teacher the recording in their own Drive, and covers the case where
        # Classroom resolves the link back into the file. Best-effort.
        for upload in uploaded:
            drive_service.grant_access(upload["file_id"], config.classroom_subject)

        classroom = classroom_service.post_material(
            subject=config.classroom_subject,
            course_id=req.get("course_id", ""),
            drive_file_ids=[u["file_id"] for u in uploaded],
            drive_links=[u["link"] for u in uploaded if u.get("link")],
            title=req.get("title") or "Class recording",
            description=req.get("description", ""),
            topic_id=req.get("topic_id") or None,
            state=req.get("post_state", "PUBLISHED"),
            scheduled_time=req.get("scheduled_time") or None,
        )

        result_payload: Dict[str, Any] = {
            "session_code": req.get("session_code"),
            "day_number": req.get("day_number"),
            "date_key": req.get("date_key"),
            "title": req.get("title"),
            "files": uploaded,
            "classroom": classroom.to_dict(),
            "published_at": datetime.now().isoformat(timespec="seconds"),
        }

        _notify_webhook(config.webhook_url, config.webhook_secret, result_payload)

        if classroom.ok:
            message = f"Published to Classroom ({len(uploaded)} video(s))."
        else:
            # Drive succeeded, so this is a partial success, not a failure.
            message = (
                f"Uploaded {len(uploaded)} video(s) to Drive. "
                f"Not posted to Classroom: {classroom.detail or classroom.reason}"
            )

        store.update_job(
            job_id,
            status="completed",
            progress=1.0,
            message=message,
            result=result_payload,
        )
        logger.info(f"[PUBLISH] Job {job_id} completed (classroom ok={classroom.ok})")

    except Exception as e:                          # noqa: BLE001 - surface, never crash
        logger.error(f"[PUBLISH] Job {job_id} failed: {e}", exc_info=True)
        store.update_job(
            job_id,
            status="failed",
            error=str(e),
            message=f"Publish failed: {e}",
            result={"files": uploaded} if uploaded else None,
        )

    finally:
        if trimmer:
            try:
                trimmer.cleanup()
            except Exception:                       # noqa: BLE001
                pass
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
