"""
Per-class publishing settings.

One entry per class (Session 127, 128, ...). Everything the publish flow needs
to turn a Zoom recording into a finished post lives here, so the per-recording
screen only ever asks you to confirm — never to re-enter the same answers.

Stored as JSON next to the job database, which on Render sits on the mounted
disk (see render.yaml), so settings survive deploys.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()

# Colors mirror the publish UI so a class reads the same in both places.
PALETTE = ["teal", "blue", "plum", "amber", "green"]

DEFAULT_FILENAME_PATTERN = "Session {session} - Day {day} - {date} ({view}).mp4"
DEFAULT_TITLE_PATTERN = "{course} — Day {day} ({date})"

# Zoom recording_type -> our short key.
#
# Zoom's own names are ambiguous: "shared_screen_with_gallery_view" DOES include
# the shared screen (it's the screen plus a strip of faces), while plain
# "gallery_view" is the one with no screen share at all. The labels below say
# which is which in plain words so nobody has to guess from the raw type.
VIEW_TYPES: Dict[str, Dict[str, str]] = {
    "speaker": {
        "name": "Shared screen + active speaker",
        "description": "What you share, with whoever is talking in a small tile. The usual one.",
        "zoom_type": "shared_screen_with_speaker_view",
        "folder": "Speaker + Screenshare",
    },
    "gallery": {
        "name": "Shared screen + gallery of faces",
        "description": "What you share, with a strip of everyone's cameras alongside it.",
        "zoom_type": "shared_screen_with_gallery_view",
        "folder": "Gallery + Screenshare",
    },
    "gallery_only": {
        "name": "Gallery of faces only",
        "description": "Just the grid of cameras — no shared screen at all.",
        "zoom_type": "gallery_view",
        "folder": "Gallery Only",
    },
    "screen_only": {
        "name": "Shared screen only",
        "description": "Just what was shared — no cameras.",
        "zoom_type": "shared_screen",
        "folder": "Screen Only",
    },
    "active": {
        "name": "Active speaker only",
        "description": "Just the camera of whoever is talking — no shared screen.",
        "zoom_type": "active_speaker",
        "folder": "Active Speaker",
    },
    "audio": {
        "name": "Audio only",
        "description": "Sound with no picture.",
        "zoom_type": "audio_only",
        "folder": "Audio",
    },
}

PRIMARY_VIEW = "speaker"


def _config_path() -> str:
    """
    Where settings live on disk.

    On Render this sits inside the mounted disk declared in render.yaml. If that
    disk isn't actually attached to the service, the directory is ephemeral and
    every deploy wipes it — which looks like "it made me reconnect again".
    Set PUBLISH_CONFIG_PATH to point somewhere durable instead.
    """
    override = os.getenv("PUBLISH_CONFIG_PATH")
    if override:
        return override
    db = os.getenv("UPLOAD_JOBS_DB", "data/upload_jobs.db")
    return os.path.join(os.path.dirname(db) or ".", "publish_classes.json")


def storage_status() -> Dict[str, Any]:
    """
    Where settings are stored and whether that survives a deploy, so the UI can
    say so plainly rather than leaving someone to notice they've been logged
    out again.
    """
    from services.config_store import get_config_store
    status = dict(get_config_store().describe())
    status["env_fallback_in_use"] = bool(os.getenv("CLASSROOM_SUBJECT"))
    return status


@dataclass
class ClassSettings:
    """Everything that is decided once per class rather than per recording."""

    code: str                                   # "127"
    label: str = ""                             # "Session 127 — Mon/Wed/Fri Night"
    color: str = "teal"

    # --- schedule, used to propose the trim and the day number ---
    timezone: str = "America/New_York"
    scheduled_start: str = ""                   # "23:00" local
    scheduled_end: str = ""                     # "02:00" local (may cross midnight)
    meeting_weekdays: List[int] = field(default_factory=list)  # 0=Mon .. 6=Sun
    first_class_date: str = ""                  # "2025-11-10", day 1
    # Keep 5 minutes before the scheduled start and 10 after the scheduled end.
    # Classes run over more often than they start early, and the end is clamped
    # to the real recording length, so asking for 10 costs nothing when it isn't
    # there.
    pad_before_minutes: int = 5
    pad_after_minutes: int = 10

    # --- what to publish ---
    views: List[str] = field(default_factory=lambda: [PRIMARY_VIEW])
    filename_pattern: str = DEFAULT_FILENAME_PATTERN
    title_pattern: str = DEFAULT_TITLE_PATTERN

    # --- where it goes ---
    drive_folder_id: str = ""                   # blank = the app's shared folder
    classroom_course_id: str = ""
    classroom_course_name: str = ""
    classroom_topic_id: str = ""
    classroom_topic_name: str = ""
    post_state: str = "PUBLISHED"               # or DRAFT
    share_mode: str = "VIEW"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClassSettings":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    # -- schedule maths ---------------------------------------------------

    def scheduled_duration_minutes(self) -> Optional[int]:
        """Minutes between scheduled_start and scheduled_end, midnight-aware."""
        if not self.scheduled_start or not self.scheduled_end:
            return None
        try:
            sh, sm = (int(x) for x in self.scheduled_start.split(":"))
            eh, em = (int(x) for x in self.scheduled_end.split(":"))
        except (ValueError, AttributeError):
            return None
        start = sh * 60 + sm
        end = eh * 60 + em
        if end <= start:            # crosses midnight, e.g. 23:00 -> 02:00
            end += 24 * 60
        return end - start

    def day_number_for(self, meeting_date: date) -> Optional[int]:
        """
        Which class day this date is — the Nth meeting since first_class_date.

        Counts scheduled meeting days rather than reading a spreadsheet, so it
        cannot silently return the wrong number. Returns None when the class
        has no schedule configured or the date isn't a meeting day.
        """
        if not self.first_class_date or not self.meeting_weekdays:
            return None
        try:
            start = datetime.strptime(self.first_class_date, "%Y-%m-%d").date()
        except ValueError:
            return None
        if meeting_date < start:
            return None
        if meeting_date.weekday() not in self.meeting_weekdays:
            return None

        count = 0
        cursor = start
        # Bounded: a class runs weeks, not years. 400 days is a generous cap.
        for _ in range(400):
            if cursor > meeting_date:
                break
            if cursor.weekday() in self.meeting_weekdays:
                count += 1
                if cursor == meeting_date:
                    return count
            cursor += timedelta(days=1)
        return None


@dataclass
class PublishConfig:
    """Top-level publishing settings."""

    classes: Dict[str, ClassSettings] = field(default_factory=dict)
    classroom_subject: str = ""      # teacher the backend impersonates
    webhook_url: str = ""            # optional: your own service, posted to on success
    webhook_secret: str = ""
    # Used for recordings that aren't matched to a class. Without it those fall
    # back to UTC, which dates a late-night class to the following day.
    default_timezone: str = "America/New_York"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "classes": {k: v.to_dict() for k, v in self.classes.items()},
            "classroom_subject": self.classroom_subject,
            "webhook_url": self.webhook_url,
            "webhook_secret": self.webhook_secret,
            "default_timezone": self.default_timezone,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PublishConfig":
        return cls(
            classes={
                k: ClassSettings.from_dict(v)
                for k, v in (data.get("classes") or {}).items()
            },
            classroom_subject=data.get("classroom_subject", "") or "",
            webhook_url=data.get("webhook_url", "") or "",
            webhook_secret=data.get("webhook_secret", "") or "",
            default_timezone=data.get("default_timezone") or "America/New_York",
        )


_cache: Optional[PublishConfig] = None


def load() -> PublishConfig:
    """Read config from disk (cached). Missing file is not an error."""
    global _cache
    if _cache is not None:
        return _cache

    from services.config_store import get_config_store

    store = get_config_store()
    raw = store.read()
    if raw is None:
        logger.info(f"[PUBLISH] No settings stored yet (backend: {store.name})")
        _cache = _apply_env_fallbacks(PublishConfig())
        return _cache

    try:
        _cache = PublishConfig.from_dict(raw)
        logger.info(
            f"[PUBLISH] Loaded {len(_cache.classes)} class setting(s) "
            f"from {store.name}"
        )
    except Exception as e:                          # noqa: BLE001 - never 500 on bad data
        logger.error(f"[PUBLISH] Stored settings were unreadable: {e}. Starting empty.")
        _cache = PublishConfig()
    _cache = _apply_env_fallbacks(_cache)
    return _cache


def _apply_env_fallbacks(config: PublishConfig) -> PublishConfig:
    """
    Environment variables fill in account-level settings the stored file
    doesn't have. They survive a wiped disk, so setting CLASSROOM_SUBJECT on
    the service means the Classroom connection never needs re-entering even if
    the settings file is lost.
    """
    if not config.classroom_subject and os.getenv("CLASSROOM_SUBJECT"):
        config.classroom_subject = os.environ["CLASSROOM_SUBJECT"].strip()
        logger.info("[PUBLISH] Using CLASSROOM_SUBJECT from the environment")
    if not config.webhook_url and os.getenv("PUBLISH_WEBHOOK_URL"):
        config.webhook_url = os.environ["PUBLISH_WEBHOOK_URL"].strip()
    if os.getenv("PUBLISH_DEFAULT_TIMEZONE"):
        config.default_timezone = os.environ["PUBLISH_DEFAULT_TIMEZONE"].strip()
    return config


def save(config: PublishConfig) -> None:
    """Persist config through the configured store and refresh the cache."""
    global _cache
    from services.config_store import get_config_store

    store = get_config_store()
    with _LOCK:
        ok = store.write(config.to_dict())
        _cache = config
    if ok:
        logger.info(
            f"[PUBLISH] Saved {len(config.classes)} class setting(s) to {store.name}"
        )
    else:
        # The caller already has the values in memory; make the failure loud so
        # it doesn't look saved when it isn't.
        logger.error(f"[PUBLISH] FAILED to persist settings to {store.name}")
        raise RuntimeError(
            f"Settings could not be saved to {store.name}. They are active for now "
            f"but will be lost on restart."
        )


def get_class(session_code: str) -> Optional[ClassSettings]:
    return load().classes.get(str(session_code))


def upsert_class(settings: ClassSettings) -> ClassSettings:
    config = load()
    if not settings.color or settings.color not in PALETTE:
        settings.color = PALETTE[len(config.classes) % len(PALETTE)]
    if not settings.label:
        settings.label = f"Session {settings.code}"
    config.classes[settings.code] = settings
    save(config)
    return settings


def delete_class(session_code: str) -> bool:
    config = load()
    if session_code not in config.classes:
        return False
    del config.classes[session_code]
    save(config)
    return True


def reset_cache() -> None:
    """Drop the cache — used by tests."""
    global _cache
    _cache = None
