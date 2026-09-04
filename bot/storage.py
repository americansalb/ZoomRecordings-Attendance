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
    async def upload(self, *, data: bytes, filename: str, session_folder: str,
                     subfolder: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
        """Return (file_id, web_view_link); (None, None) if nothing was stored.

        `subfolder` files the image one level deeper, inside the session
        folder: that is how one student's photos end up together in a
        folder of their own instead of scattered through the session.
        """


class NullStorage(Storage):
    @property
    def stores_images(self) -> bool:
        return False

    async def upload(self, *, data: bytes, filename: str, session_folder: str,
                     subfolder: Optional[str] = None):
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

    def _ensure_folder(self, name: str, parent_id: Optional[str] = None) -> str:
        """The folder `name` under `parent_id` (our configured parent when
        none is given), created once and remembered. Cached by the full
        path so two students with the same name in different sessions
        cannot collide onto one folder."""
        parent = parent_id if parent_id is not None else self.parent_folder_id
        cache_key = f"{parent or 'root'}/{name}"
        with self._lock:
            if cache_key in self._folder_cache:
                return self._folder_cache[cache_key]
            svc = self._get_service()
            meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
            if parent:
                meta["parents"] = [parent]
            folder = svc.files().create(
                body=meta, fields="id", supportsAllDrives=True
            ).execute()
            self._folder_cache[cache_key] = folder["id"]
            return folder["id"]

    def _upload_sync(self, data: bytes, filename: str, session_folder: str,
                     subfolder: Optional[str] = None) -> Tuple[str, str]:
        from googleapiclient.http import MediaInMemoryUpload

        svc = self._get_service()
        folder_id = self._ensure_folder(session_folder)
        if subfolder:
            folder_id = self._ensure_folder(subfolder, parent_id=folder_id)
        media = MediaInMemoryUpload(data, mimetype="image/png")
        f = svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        ).execute()
        return f["id"], f.get("webViewLink")

    async def upload(self, *, data: bytes, filename: str, session_folder: str,
                     subfolder: Optional[str] = None):
        import asyncio
        try:
            return await asyncio.to_thread(
                self._upload_sync, data, filename, session_folder, subfolder)
        except Exception as e:
            logger.warning("Drive upload failed for %s: %s", filename, e)
            return (None, None)


def purge_drive_older_than(parent_folder_id: Optional[str], days: int) -> int:
    """Delete session folders older than `days` under OUR parent folder only.

    The fence is deliberate: the service account may be able to see files
    that are not ours, and a janitor without a fence must not exist, so a
    missing parent folder means no purge at all. Files inside a stale
    session folder are deleted first, then the folder, so nothing is left
    orphaned.
    """
    if not parent_folder_id or days <= 0:
        return 0
    import datetime

    svc = DriveStorage(parent_folder_id=parent_folder_id)._get_service()
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    q = (f"'{parent_folder_id}' in parents"
         " and mimeType='application/vnd.google-apps.folder'"
         f" and createdTime < '{cutoff}' and trashed=false")
    deleted = 0
    page_token = None
    while True:
        resp = svc.files().list(
            q=q, fields="nextPageToken, files(id, name)", pageToken=page_token,
            pageSize=50, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        for folder in resp.get("files", []):
            child_token = None
            while True:
                children = svc.files().list(
                    q=f"'{folder['id']}' in parents and trashed=false",
                    fields="nextPageToken, files(id)", pageToken=child_token,
                    pageSize=100, supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                ).execute()
                for f in children.get("files", []):
                    svc.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
                    deleted += 1
                child_token = children.get("nextPageToken")
                if not child_token:
                    break
            svc.files().delete(fileId=folder["id"], supportsAllDrives=True).execute()
            deleted += 1
            logger.info("retention: removed expired session folder %r", folder.get("name"))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return deleted


def build_storage(store_images: bool, parent_folder_id: Optional[str]) -> Storage:
    if not store_images:
        return NullStorage()
    return DriveStorage(parent_folder_id=parent_folder_id)
