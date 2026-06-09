"""
Screenshot storage.

  - NullStorage: keeps nothing (presence-flags-only / log-only mode).
  - DriveStorage: uploads each frame to a per-session folder in Google Drive and
    returns (file_id, web_view_link).

Google imports are lazy so the rest of the bot (and its tests) don't require the
Google client libraries.
"""

from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class Storage(ABC):
    @property
    @abstractmethod
    def stores_images(self) -> bool: ...

    @abstractmethod
    async def upload(self, *, data: bytes, filename: str, session_folder: str) -> Tuple[Optional[str], Optional[str]]:
        """Return (file_id, web_view_link); (None, None) if nothing was stored."""


class NullStorage(Storage):
    @property
    def stores_images(self) -> bool:
        return False

    async def upload(self, *, data: bytes, filename: str, session_folder: str):
        return (None, None)


class DriveStorage(Storage):
    """Google Drive uploader. One folder per session, cached by name."""

    def __init__(self, parent_folder_id: Optional[str] = None):
        self.parent_folder_id = parent_folder_id
        self._service = None
        self._folder_cache: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def stores_images(self) -> bool:
        return True

    def _get_service(self):
        if self._service is None:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            scopes = ["https://www.googleapis.com/auth/drive"]
            sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
            if sa_file and os.path.exists(sa_file):
                creds = service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)
            else:
                info = {
                    "type": "service_account",
                    "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
                    "private_key": (os.getenv("GOOGLE_PRIVATE_KEY") or "").replace("\\n", "\n"),
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
                creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _ensure_folder(self, name: str) -> str:
        with self._lock:
            if name in self._folder_cache:
                return self._folder_cache[name]
            svc = self._get_service()
            meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
            if self.parent_folder_id:
                meta["parents"] = [self.parent_folder_id]
            folder = svc.files().create(
                body=meta, fields="id", supportsAllDrives=True
            ).execute()
            self._folder_cache[name] = folder["id"]
            return folder["id"]

    def _upload_sync(self, data: bytes, filename: str, session_folder: str) -> Tuple[str, str]:
        from googleapiclient.http import MediaInMemoryUpload

        svc = self._get_service()
        folder_id = self._ensure_folder(session_folder)
        media = MediaInMemoryUpload(data, mimetype="image/png")
        f = svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        return f["id"], f.get("webViewLink")

    async def upload(self, *, data: bytes, filename: str, session_folder: str):
        import asyncio
        try:
            return await asyncio.to_thread(self._upload_sync, data, filename, session_folder)
        except Exception as e:
            logger.warning("Drive upload failed for %s: %s", filename, e)
            return (None, None)


def build_storage(store_images: bool, parent_folder_id: Optional[str]) -> Storage:
    if not store_images:
        return NullStorage()
    return DriveStorage(parent_folder_id=parent_folder_id)
