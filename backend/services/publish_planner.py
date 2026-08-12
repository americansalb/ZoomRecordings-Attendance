"""
Turns a Zoom recording into a publish plan.

Everything the UI shows for a recording — which class, which day, where to cut,
which files get created, where they land — is computed here so the frontend
holds no business logic. The old flow did this arithmetic in React with
console.logs and magic thresholds; this is the same idea with the maths in one
testable place.

Nothing here performs I/O against Zoom or Google.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.class_config import (
    PRIMARY_VIEW,
    VIEW_TYPES,
    ClassSettings,
    PublishConfig,
    normalize_zoom_type,
    view_key_for,
)

logger = logging.getLogger(__name__)

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(f"[PLAN] Unknown timezone {name!r}, falling back to UTC")
        return ZoneInfo("UTC")


def extract_session_code(title: str) -> Optional[str]:
    match = re.search(r"Session\s*(\d{3})", title or "", re.IGNORECASE)
    return match.group(1) if match else None


def format_time(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


UNSORTED_FOLDER = "Unsorted"


def safe_filename(text: str, limit: int = 80) -> str:
    """Make a Zoom topic usable as a filename without mangling it beyond recognition."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", text or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:limit].rstrip() or "Recording")


def _fill(pattern: str, values: Dict[str, Any], fallback: str) -> str:
    """Render a {token} pattern, tolerating typos rather than exploding."""
    try:
        return pattern.format(**values)
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"[PLAN] Bad pattern {pattern!r}: {e}. Using default.")
        try:
            return fallback.format(**values)
        except Exception:                           # noqa: BLE001
            return values.get("session", "recording")


def compute_trim(
    settings: ClassSettings,
    recording_start_utc: datetime,
    video_duration_seconds: float,
) -> Dict[str, Any]:
    """
    Where to cut, based on when the class was actually scheduled.

    Zoom starts recording when the host opens the room, which is usually before
    class and long after it ends. The scheduled window plus each class's own
    padding gives the cut. Returns full-length bounds when the class has no
    schedule configured, so a missing setting never produces a silly cut.
    """
    duration_minutes = settings.scheduled_duration_minutes()
    if not settings.scheduled_start or duration_minutes is None:
        return {
            "start_seconds": 0.0,
            "end_seconds": video_duration_seconds,
            "source": "full",
            "note": "No schedule set for this class — keeping the whole recording.",
        }

    tz = _zone(settings.timezone)
    local_start = recording_start_utc.astimezone(tz)

    try:
        hour, minute = (int(x) for x in settings.scheduled_start.split(":"))
    except ValueError:
        return {
            "start_seconds": 0.0,
            "end_seconds": video_duration_seconds,
            "source": "full",
            "note": "Scheduled start time is malformed — keeping the whole recording.",
        }

    scheduled = local_start.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # A recording that begins just after midnight belongs to the previous
    # evening's class (an 11pm class recorded at 00:05). Pick whichever
    # candidate the recording actually sits closest to.
    candidates = [scheduled, scheduled - timedelta(days=1), scheduled + timedelta(days=1)]
    scheduled = min(candidates, key=lambda c: abs((local_start - c).total_seconds()))

    offset = (scheduled - local_start).total_seconds()

    # Sanity: the room shouldn't open more than 4h early or start more than
    # 30 min late. Outside that, the schedule probably doesn't match this
    # recording, so don't pretend to know better than the full video.
    if offset < -1800 or offset > 14400:
        return {
            "start_seconds": 0.0,
            "end_seconds": video_duration_seconds,
            "source": "unmatched",
            "note": (
                f"Recording started {abs(offset) / 60:.0f} min "
                f"{'after' if offset < 0 else 'before'} the scheduled class time, "
                "which looks wrong — keeping the whole recording."
            ),
        }

    start = max(0.0, offset - settings.pad_before_minutes * 60)
    end = offset + duration_minutes * 60 + settings.pad_after_minutes * 60
    end = min(video_duration_seconds, end)
    if end <= start:
        return {
            "start_seconds": 0.0,
            "end_seconds": video_duration_seconds,
            "source": "full",
            "note": "Computed an empty cut — keeping the whole recording.",
        }

    return {
        "start_seconds": round(start, 2),
        "end_seconds": round(end, 2),
        "source": "schedule",
        "note": (
            f"Class runs {settings.scheduled_start}–{settings.scheduled_end}. "
            f"Keeping {settings.pad_before_minutes} min before and "
            f"{settings.pad_after_minutes} min after."
        ),
    }


# Zoom returns transcripts, chat logs and timeline JSON in the same list as the
# videos. Only these two file types are a recording of the class.
MEDIA_FILE_TYPES = {"MP4", "M4A"}

# Key prefix for a video whose recording_type we have no name for. It stays
# sendable rather than being dropped: an unrecognised type is exactly how a
# class once got published with no shared screen and nothing saying why.
UNKNOWN_VIEW_PREFIX = "other_"


def _describe_unknown(raw_type: str) -> Dict[str, str]:
    """A name, a folder and an honest description for a type we don't know."""
    words = re.sub(r"[^a-z0-9]+", " ", (raw_type or "").lower()).strip()
    label = words.title() if words else "Unnamed video"
    return {
        "name": label,
        "description": (
            f'Zoom calls this "{raw_type}". We have no plain-English name for it, '
            f"but it is a video and it can be sent."
        ),
        "folder": label,
    }


def collect_views(files: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Every video Zoom produced for one recording, keyed by our view names.

    Two things this does that comparing `recording_type` strings did not:

      * It matches Zoom's decorated names. A class recorded with closed captions
        comes back as `shared_screen_with_speaker_view(CC)`, which used to match
        nothing — the screen share existed and the publish screen said it didn't.
      * It keeps a video whose type we don't recognise, under a name built from
        Zoom's own, instead of dropping it. Whatever Zoom calls it next, it stays
        visible and sendable.

    Ordered as VIEW_TYPES is (the everyday one first), with anything unrecognised
    after it.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        file_type = (f.get("file_type") or "").upper()
        raw_type = (f.get("recording_type") or "").strip()
        key = view_key_for(raw_type)
        if key is None:
            # An unrecognised type is only worth offering if it is plainly a
            # video: a transcript or a chat log is not the class.
            if file_type != "MP4":
                continue
            key = UNKNOWN_VIEW_PREFIX + (normalize_zoom_type(raw_type) or "video")
            logger.info(f"[PLAN] Zoom sent a video type we don't name: {raw_type!r}")
        elif file_type and file_type not in MEDIA_FILE_TYPES:
            continue
        grouped.setdefault(key, []).append(f)

    ordered = [k for k in VIEW_TYPES if k in grouped]
    ordered += sorted(k for k in grouped if k not in VIEW_TYPES)

    available: Dict[str, Dict[str, Any]] = {}
    for key in ordered:
        # A recording that was stopped and restarted mid-class comes back as
        # several files of the same type. Send the longest one that can actually
        # be downloaded, and count the rest so the screen can say so — taking
        # whichever Zoom listed first threw part of the class away in silence.
        matches = sorted(
            grouped[key],
            key=lambda f: (bool(f.get("download_url")), f.get("file_size") or 0),
            reverse=True,
        )
        best = matches[0]
        spec = VIEW_TYPES.get(key) or _describe_unknown(best.get("recording_type", ""))
        available[key] = {
            "key": key,
            "name": spec["name"],
            "description": spec.get("description", ""),
            # Zoom's own value, not our canonical one, so the frontend can hand
            # this exact file back when it asks for a replan.
            "zoom_type": best.get("recording_type") or spec.get("zoom_type", ""),
            "folder": spec["folder"],
            "file_id": best.get("id"),
            "download_url": best.get("download_url"),
            "size_bytes": best.get("file_size") or 0,
            "part_count": len(matches),
        }
    return available


def _sentence_list(items: List[str]) -> str:
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


DEFAULT_CLASS_MINUTES = 180        # classes run three hours
DEFAULT_PAD_BEFORE_MINUTES = 5     # keep five minutes before the start
DEFAULT_PAD_AFTER_MINUTES = 10     # and ten after the end, where the recording has them


def build_title(
    settings: Optional[ClassSettings],
    tokens: Dict[str, Any],
    topic: str,
    date_key: str,
    day_number: Optional[int],
) -> str:
    """
    The title students see.

    Two guarantees, in this order:
      1. The recording date is always in it. Even with no class, no day number
         and no settings, you can still tell one recording from another.
      2. "Day N" only appears when N is actually known — never "Day None".
    """
    if settings:
        pattern = settings.title_pattern
        if day_number is None:
            # Strip the day fragment rather than rendering "Day None".
            pattern = re.sub(r"\s*[-—–|]?\s*Day\s*\{day\}", "", pattern).strip()
        title = _fill(pattern, tokens, "{course} — Day {day} ({date})").strip()
    else:
        title = (topic or "Class recording").strip()
        # A day typed by hand still belongs in the title, class settings or not.
        if day_number is not None:
            title = f"{title} — Day {day_number}"

    # Belt and braces: whatever the pattern did, the date ends up in the title.
    if date_key.lower() not in title.lower():
        title = f"{title} ({date_key})"

    return re.sub(r"\s{2,}", " ", title).strip(" —-|")


def manual_trim_settings(
    start_local: str,
    duration_minutes: int = DEFAULT_CLASS_MINUTES,
    timezone: str = "America/New_York",
) -> ClassSettings:
    """
    A throwaway ClassSettings representing "the class started at HH:MM and ran
    for N minutes", so a recording with no class can still be trimmed properly
    by the same code path as a configured one.
    """
    try:
        hour, minute = (int(x) for x in start_local.split(":"))
    except (ValueError, AttributeError):
        raise ValueError(f"Start time must look like 14:30, got {start_local!r}")

    end_total = (hour * 60 + minute + duration_minutes) % (24 * 60)
    return ClassSettings(
        code="",
        timezone=timezone,
        scheduled_start=f"{hour:02d}:{minute:02d}",
        scheduled_end=f"{end_total // 60:02d}:{end_total % 60:02d}",
        pad_before_minutes=DEFAULT_PAD_BEFORE_MINUTES,
        pad_after_minutes=DEFAULT_PAD_AFTER_MINUTES,
    )


def plan_recording(
    recording: Dict[str, Any],
    config: PublishConfig,
    day_override: Optional[int] = None,
    session_override: Optional[str] = None,
    manual_start: Optional[str] = None,
    manual_duration_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build the full plan for one Zoom recording.

    The returned dict is exactly what the publish screen renders, and the same
    shape the worker consumes — one source of truth for both.
    """
    topic = recording.get("topic", "") or ""
    session_code = session_override or extract_session_code(topic)
    settings = config.classes.get(session_code) if session_code else None

    start_utc = _parse_iso(recording.get("start_time", "")) or datetime.now(tz=ZoneInfo("UTC"))
    # Unmatched recordings still need a real local date: falling back to UTC
    # dates an 11pm class to the following day in every filename.
    tz = _zone(settings.timezone if settings else config.default_timezone)
    local = start_utc.astimezone(tz)

    # Zoom reports duration in whole minutes; the worker re-clamps against the
    # real duration once ffprobe has seen the file.
    duration_seconds = float(recording.get("duration") or 0) * 60

    available = collect_views(recording.get("recording_files") or [])

    # Screen + speaker is what nearly every class sends, so it's the default
    # when the class hasn't said otherwise, and the fallback when what the class
    # asked for isn't among the files Zoom produced. Failing both, preselect
    # whatever there is — an empty selection means a disabled Send button and no
    # explanation of why.
    requested = list(settings.views) if settings else [PRIMARY_VIEW]
    wanted = [v for v in requested if v in available]
    if not wanted:
        if PRIMARY_VIEW in available:
            wanted = [PRIMARY_VIEW]
        elif available:
            wanted = [next(iter(available))]

    # Falling back is fine; falling back without saying so is what left a class
    # published as gallery-only. Name what's missing and where to go and turn it
    # on, rather than leaving someone to wonder why the option isn't there.
    missing_names = [VIEW_TYPES[v]["name"] for v in requested
                     if v not in available and v in VIEW_TYPES]
    view_note = ""
    if available and missing_names:
        view_note = (
            f"Zoom didn't produce {_sentence_list(missing_names).lower()} for this "
            f"recording, so it isn't in the list below. If you expected it, turn that "
            f"layout on in the cloud recording settings of the Zoom account that hosts "
            f"this class — it can't be recovered for a class already recorded."
        )

    day_number = day_override
    if day_number is None and settings:
        day_number = settings.day_number_for(local.date())

    date_key = f"{MONTHS[local.month - 1]}{local.day}"
    date_label = local.strftime("%a %b %-d") if hasattr(local, "strftime") else str(local.date())

    # Trim priority: a start time typed by hand beats the class schedule, which
    # beats keeping the whole recording.
    if manual_start and duration_seconds > 0:
        manual = manual_trim_settings(
            manual_start,
            manual_duration_minutes or DEFAULT_CLASS_MINUTES,
            settings.timezone if settings else config.default_timezone,
        )
        trim = compute_trim(manual, start_utc, duration_seconds)
        trim["source"] = "manual"
        trim["note"] = (
            f"Class started {manual_start} and ran "
            f"{(manual_duration_minutes or DEFAULT_CLASS_MINUTES) // 60}h"
            f"{(manual_duration_minutes or DEFAULT_CLASS_MINUTES) % 60:02d}m. "
            f"Keeping {DEFAULT_PAD_BEFORE_MINUTES} min before and "
            f"{DEFAULT_PAD_AFTER_MINUTES} min after."
        )
    elif settings and duration_seconds > 0:
        trim = compute_trim(settings, start_utc, duration_seconds)
    else:
        trim = {
            "start_seconds": 0.0,
            "end_seconds": duration_seconds,
            "source": "full",
            "note": "Whole recording — tell us when the class started to trim it.",
        }

    tokens = {
        "session": session_code or "___",
        "day": day_number if day_number is not None else "_",
        "date": date_key,
        "course": (settings.classroom_course_name or settings.label) if settings else "",
        "view": "",
    }

    # Where files land. A recording with no class still has somewhere to go —
    # an Unsorted folder named after the Zoom topic — so publishing never
    # depends on setting a class up first.
    root_folder = f"Session {session_code}" if settings else UNSORTED_FOLDER

    # Name EVERY available view, not just the ones selected by default —
    # otherwise ticking an extra view in the UI sends a file with no filename
    # and previews a blank destination.
    for view in available.values():
        folder = view["folder"]
        if settings:
            filename = _fill(
                settings.filename_pattern,
                {**tokens, "view": folder},
                "Session {session} - Day {day} - {date} ({view}).mp4",
            )
        else:
            # No class: keep Zoom's own title so the file is still identifiable,
            # and include the day if one was typed in.
            day_part = f" - Day {day_number}" if day_number is not None else ""
            filename = f"{safe_filename(topic)}{day_part} - {date_key} ({folder}).mp4"

        view["filename"] = filename
        view["drive_folders"] = [root_folder, folder]

    outputs = [available[key] for key in wanted]

    title = build_title(settings, tokens, topic, date_key, day_number)

    # Advisory, not gating. These say what's unresolved, and the UI offers to
    # resolve them — but a recording can always be uploaded to Drive as-is.
    blockers: List[str] = []
    if not session_code:
        blockers.append("no_session_code")
    elif not settings:
        blockers.append("class_not_configured")
    if settings and day_number is None:
        blockers.append("no_day_number")
    if not available:
        blockers.append("no_video_files")

    return {
        "recording_id": recording.get("id"),
        "meeting_id": recording.get("meeting_id"),
        "topic": topic,
        "host_name": recording.get("host_name", ""),
        "start_time": recording.get("start_time"),
        "date_key": date_key,
        "started_local": local.strftime("%H:%M"),
        "date_label": date_label,
        "duration_seconds": duration_seconds,
        "total_size_bytes": sum(v["size_bytes"] for v in available.values()),

        "session_code": session_code,
        "class_label": settings.label if settings else None,
        "class_color": settings.color if settings else "amber",
        "day_number": day_number,

        "trim": trim,
        "available_views": list(available.values()),
        "outputs": outputs,
        "view_note": view_note,

        "title": title,
        "course_id": settings.classroom_course_id if settings else "",
        "course_name": settings.classroom_course_name if settings else "",
        "topic_id": settings.classroom_topic_id if settings else "",
        "topic_name": settings.classroom_topic_name if settings else "",
        "post_state": settings.post_state if settings else "PUBLISHED",

        "drive_root": root_folder,
        "blockers": blockers,
        # Fully resolved: class known, day known, files present.
        "ready": not blockers,
        # Anything with a video can be uploaded to Drive right now, class or no class.
        "can_send": bool(available),
    }
