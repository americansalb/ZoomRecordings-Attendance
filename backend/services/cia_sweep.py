"""
CIA auto-upload: Zoom cloud recordings of Consecutive Interpreting
Assessments land in the grading wizard's Drive intake folder by themselves.

The owner's ask (2026-09-08): the class uploader already moves class
sessions into their Drive folders; exams should ride the same machinery so
nobody downloads a 500 MB MP4 from Zoom just to upload it back to Drive.
A recording is an exam when its title carries the word CIA or the phrase
"consecutive interpreting assessment" (the letters also say
"interpretation", so both spellings count).

How a sweep works, in order, per recording that matches:
  1. Skip it if a Drive folder stamped with this recording's Zoom UUID
     already exists anywhere the service account can see. The stamp lives
     in Drive itself (appProperties), so it survives the wizard moving the
     folder to "graded", the uploader restarting, and the free-tier disk
     being wiped.
  2. Skip it (until the next sweep) while Zoom is still processing the
     files.
  3. Make one subfolder per exam in the intake folder, named from the Zoom
     topic and the Eastern date, download the best video (and the
     audio-only copy when Zoom made one), and upload both into it.
  4. Stamp the folder with the UUID only after every upload succeeded, so
     a half-delivered exam is finished by the next sweep instead of
     abandoned. Uploads replace in place, so retries never duplicate.

Exam recordings are NEVER link-shared: unlike class recordings, nothing
here calls the anyone-with-the-link permission. The wizard's service
account reads the file directly.

The sweep is triggered by whoever pokes POST /api/cia/sweep (the
attendance bot pings it on a timer, because this API naps on the free
tier and the bot's box is always awake) and by the in-process scheduler
while the API happens to be up. Both paths land here, and the lock makes
overlapping triggers harmless.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import re

import requests

from services.publish_planner import MONTHS, safe_filename

logger = logging.getLogger(__name__)

# The grading wizard's intake folder (its "to-do list"). The default is the
# wizard's own default browse root in the learn repo
# (services/cia-wizard/drive-source.js); override per environment when the
# wizard is pointed somewhere else.
DEFAULT_CIA_FOLDER = "17qcBP9W_OSJkiA2dQkw0jLJnYEz7nca3"


def cia_folder_id() -> str:
    return os.getenv("CIA_DRIVE_FOLDER_ID") or DEFAULT_CIA_FOLDER


def lookback_days() -> int:
    try:
        return max(1, int(os.getenv("CIA_SWEEP_LOOKBACK_DAYS", "7")))
    except ValueError:
        return 7


# "CIA" as its own word, any casing ("Marcia" and "Garcia" never match), or
# the phrase written out, with either "interpreting" or "interpretation".
_CIA_WORD = re.compile(r"\bCIA\b", re.IGNORECASE)
_CIA_PHRASE = re.compile(r"consecutive\s+interpret(?:ing|ation)\s+assessment", re.IGNORECASE)


def is_cia_topic(topic: str) -> bool:
    t = topic or ""
    return bool(_CIA_WORD.search(t) or _CIA_PHRASE.search(t))


# The one video the wizard grades from, by usefulness. Exams are one
# candidate and one examiner on camera, so the speaker views carry the
# encounter; gallery is the fallback; any completed MP4 beats nothing.
_VIDEO_PREFERENCE = [
    "shared_screen_with_speaker_view",
    "speaker_view",
    "active_speaker",
    "gallery_view",
    "shared_screen_with_gallery_view",
    "shared_screen",
]


def _completed(f: Dict[str, Any]) -> bool:
    # Zoom omits `status` on some list shapes; a file with a download URL
    # and no status is treated as ready.
    return bool(f.get("download_url")) and (f.get("status") in (None, "", "completed"))


def pick_video(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    mp4s = [f for f in (files or []) if (f.get("file_type") or "").upper() == "MP4" and _completed(f)]
    if not mp4s:
        return None
    for kind in _VIDEO_PREFERENCE:
        match = next((f for f in mp4s if f.get("recording_type") == kind), None)
        if match:
            return match
    return mp4s[0]


def pick_audio(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return next(
        (f for f in (files or [])
         if (f.get("file_type") or "").upper() == "M4A"
         and f.get("recording_type") in (None, "audio_only")
         and _completed(f)),
        None,
    )


def still_processing(files: List[Dict[str, Any]]) -> bool:
    """Zoom lists a recording before its files are downloadable."""
    return any((f.get("file_type") or "").upper() == "MP4" and not _completed(f)
               for f in (files or []))


def exam_label(topic: str, start_time_iso: str) -> str:
    """
    The folder and file name staff see in the wizard: the Zoom topic plus
    the Eastern date, because exams are scheduled in Eastern time and the
    graders talk about them that way.
    """
    label = safe_filename(topic, 60)
    try:
        start = datetime.fromisoformat((start_time_iso or "").replace("Z", "+00:00"))
        local = start.astimezone(ZoneInfo("America/New_York"))
        label = f"{label} - {MONTHS[local.month - 1]}{local.day}"
    except (ValueError, TypeError):
        pass
    return label


def _human(n: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"


def download_zoom_file(url: str, token: str, dest_path: str) -> int:
    """
    Download one Zoom recording file to disk, authenticated with the
    account's OAuth token (Zoom accepts it as a query parameter on
    recording downloads, and the redirect target it signs from that).
    Returns bytes written. Raises on anything unhealthy, including a disk
    too small for the announced size.
    """
    sep = "&" if "?" in url else "?"
    authed = f"{url}{sep}access_token={token}" if token else url

    head = requests.head(authed, allow_redirects=True, timeout=30)
    announced = int(head.headers.get("content-length", 0))
    free = shutil.disk_usage(os.path.dirname(dest_path)).free
    if announced and free < int(announced * 1.2):
        raise RuntimeError(
            f"Not enough disk for this recording ({_human(announced)} file, "
            f"{_human(free)} free). It will be retried when space frees up."
        )

    response = requests.get(authed, stream=True, timeout=3600)
    response.raise_for_status()
    written = 0
    with open(dest_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                written += len(chunk)
    if written == 0:
        raise RuntimeError("Zoom served an empty file.")
    return written


# One sweep at a time; the second trigger of an overlap reports "already
# running" instead of doing the work twice.
_lock = asyncio.Lock()

# Advisory memory of the last sweep for the status endpoint. Lost on
# restart, which is fine: Drive itself is the durable record.
last_sweep: Dict[str, Any] = {}


async def run_cia_sweep(deps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    One pass over recent Zoom recordings on every configured account.
    Returns (and stores in `last_sweep`) a summary dict. Never raises for
    per-recording problems; they land in the summary's errors list.

    `deps` is an injection seam for tests: zoom, drive, and download can
    each be swapped for fakes. Production callers pass nothing.
    """
    if os.getenv("CIA_SWEEP_DISABLED"):
        return {"disabled": True}

    if _lock.locked():
        return {"already_running": True, **({"last": last_sweep} if last_sweep else {})}

    async with _lock:
        deps = deps or {}
        if "zoom" in deps:
            zoom = deps["zoom"]
        else:
            from services.zoom_service import zoom_service as zoom
        if "drive" in deps:
            drive = deps["drive"]
        else:
            from services.drive_service import drive_service as drive
        download: Callable[[str, str, str], int] = deps.get("download", download_zoom_file)

        folder = cia_folder_id()
        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=lookback_days())).strftime("%Y-%m-%d")

        summary: Dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "window": f"{from_date} to {to_date}",
            "intake_folder": folder,
            "scanned": 0, "matched": 0, "delivered": [], "already_there": 0,
            "waiting_on_zoom": 0, "errors": [],
        }

        accounts = [a["id"] for a in zoom.get_accounts()] or []
        for account_id in accounts:
            try:
                recordings = await zoom.list_all_recordings(from_date, to_date, account_id)
            except Exception as e:                          # noqa: BLE001
                summary["errors"].append(f"Zoom listing failed on {account_id}: {e}")
                continue
            summary["scanned"] += len(recordings)

            for rec in recordings:
                topic = rec.get("topic", "") or ""
                if not is_cia_topic(topic):
                    continue
                summary["matched"] += 1
                uuid = str(rec.get("uuid") or rec.get("id") or "")
                if not uuid:
                    summary["errors"].append(f"Recording {topic!r} has no UUID; skipped.")
                    continue

                try:
                    existing = await asyncio.to_thread(
                        drive.find_by_app_property, "cia_zoom_uuid", uuid)
                    if existing:
                        summary["already_there"] += 1
                        continue

                    files = rec.get("recording_files") or []
                    video = pick_video(files)
                    if not video:
                        summary["waiting_on_zoom"] += 1
                        logger.info(f"[CIA] {topic!r}: no downloadable video yet; will retry")
                        continue
                    audio = pick_audio(files)

                    label = exam_label(topic, rec.get("start_time", ""))
                    logger.info(f"[CIA] Delivering {label!r} to the wizard's intake folder")

                    token = await zoom.get_download_token(account_id)
                    exam_folder = await asyncio.to_thread(
                        drive.get_or_create_folder, label, folder)

                    temp_dir = tempfile.mkdtemp(prefix="cia_sweep_")
                    try:
                        video_path = os.path.join(temp_dir, "exam.mp4")
                        size = await asyncio.to_thread(
                            download, video["download_url"], token, video_path)
                        logger.info(f"[CIA] {label!r}: video downloaded ({_human(size)})")
                        await asyncio.to_thread(
                            drive.upload_file_to_folder,
                            video_path, exam_folder, f"{label}.mp4", "video/mp4")
                        os.remove(video_path)

                        if audio:
                            audio_path = os.path.join(temp_dir, "exam.m4a")
                            await asyncio.to_thread(
                                download, audio["download_url"], token, audio_path)
                            await asyncio.to_thread(
                                drive.upload_file_to_folder,
                                audio_path, exam_folder, f"{label} (audio).m4a", "audio/mp4")
                    finally:
                        shutil.rmtree(temp_dir, ignore_errors=True)

                    # The stamp goes on last: a failure anywhere above leaves
                    # the folder unstamped, and the next sweep finishes the
                    # job by replacing files in place.
                    await asyncio.to_thread(
                        drive.set_app_properties, exam_folder,
                        {"cia_zoom_uuid": uuid,
                         "cia_topic": topic[:120],
                         "cia_start": rec.get("start_time", "") or ""})
                    summary["delivered"].append(label)

                except Exception as e:                      # noqa: BLE001
                    hint = ""
                    text = str(e)
                    if "404" in text or "not found" in text.lower() or "403" in text or "permission" in text.lower():
                        try:
                            sa = drive.service_account_email()
                        except Exception:                   # noqa: BLE001
                            sa = "the uploader's Google service account"
                        hint = (f" If this repeats, share the CIA intake folder "
                                f"({folder}) with {sa} as Editor.")
                    summary["errors"].append(f"{topic!r}: {e}.{hint}")
                    logger.error(f"[CIA] {topic!r} failed: {e}", exc_info=True)

        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
        last_sweep.clear()
        last_sweep.update(summary)
        logger.info(
            f"[CIA] Sweep done: {summary['matched']} matched, "
            f"{len(summary['delivered'])} delivered, {summary['already_there']} already there, "
            f"{summary['waiting_on_zoom']} still processing, {len(summary['errors'])} error(s)"
        )
        return summary
