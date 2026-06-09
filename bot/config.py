"""Bot service configuration (env-driven)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Config:
    # Where the Phase 1 backend lives (for chat events + screenshot manifests).
    backend_url: str
    bot_shared_secret: Optional[str]
    # Zoom Meeting SDK app credentials (NOT the Server-to-Server OAuth ones).
    sdk_key: Optional[str]
    sdk_secret: Optional[str]
    # How the headless browser reaches this service to load the Zoom client page.
    # Must be cross-origin-isolated (COOP/COEP) for the Web SDK's SharedArrayBuffer.
    public_base_url: str
    headless: bool
    # Google Drive parent folder for per-session screenshot folders.
    drive_folder_id: Optional[str]


def load_config() -> Config:
    return Config(
        backend_url=os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/"),
        bot_shared_secret=os.getenv("TUTOR_BOT_SHARED_SECRET"),
        sdk_key=os.getenv("ZOOM_MEETING_SDK_KEY"),
        sdk_secret=os.getenv("ZOOM_MEETING_SDK_SECRET"),
        public_base_url=os.getenv("BOT_PUBLIC_BASE_URL", "http://localhost:8088").rstrip("/"),
        headless=os.getenv("BOT_HEADLESS", "true").lower() not in ("false", "0", "no"),
        drive_folder_id=os.getenv("TUTOR_DRIVE_FOLDER_ID") or os.getenv("GOOGLE_SHARED_DRIVE_ID"),
    )
