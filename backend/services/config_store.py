"""
Where publish settings actually live.

A local file was the wrong home: on Render it sits in a directory that is wiped
on every deploy unless a paid disk is attached, so the Classroom connection kept
needing re-entering. Settings need somewhere shared and durable.

Three backends, picked automatically:

  drive  A JSON file in the same Shared Drive folder the app already uploads
         recordings to. Durable, survives every deploy, shared between the web
         service and any worker, and needs no new infrastructure — it reuses
         credentials that are already proven to work.
  redis  If REDIS_URL is set. Fast and shared, but a cache eviction loses it,
         so Drive is preferred when both are available.
  file   Local JSON. Used for local development, and as a fallback so the app
         still runs if Drive is unreachable.

Force one with PUBLISH_CONFIG_STORE=drive|redis|file.

Every backend returns None (rather than raising) when it has nothing or can't
be reached, so a storage outage degrades to "no settings yet" instead of a
500 on the settings page.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "publish-settings.json"
_CACHE_SECONDS = 30          # avoid a Drive round trip on every request


class ConfigStore(ABC):
    name = "unknown"

    @abstractmethod
    def read(self) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def write(self, data: Dict[str, Any]) -> bool: ...

    def describe(self) -> Dict[str, Any]:
        return {"backend": self.name, "durable": self.name in ("drive", "redis")}


class FileConfigStore(ConfigStore):
    """Local JSON. Fine for development; ephemeral on most hosts."""

    name = "file"

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._lock = threading.Lock()

    def read(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[CONFIG] Could not read {self.path}: {e}")
            return None

    def write(self, data: Dict[str, Any]) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._lock:
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, self.path)      # atomic; a crash can't truncate
            return True
        except OSError as e:
            logger.error(f"[CONFIG] Could not write {self.path}: {e}")
            return False

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": "file",
            "durable": False,
            "path": self.path,
            "exists": os.path.exists(self.path),
        }


class DriveConfigStore(ConfigStore):
    """
    A single JSON file in the app's Shared Drive folder.

    Durable and shared, with no extra infrastructure. The file is small and
    written rarely, so the API cost is negligible.
    """

    name = "drive"

    def __init__(self):
        self._file_id: Optional[str] = None
        self._lock = threading.Lock()

    def _drive(self):
        from services.drive_service import drive_service
        return drive_service

    def _folder_id(self) -> str:
        from services.drive_service import drive_service
        return os.getenv("PUBLISH_CONFIG_FOLDER_ID") or drive_service.SHARED_FOLDER_ID

    def _find_file(self) -> Optional[str]:
        if self._file_id:
            return self._file_id
        try:
            result = self._drive().drive.files().list(
                q=(
                    f"name='{CONFIG_FILENAME}' and '{self._folder_id()}' in parents "
                    f"and trashed=false"
                ),
                spaces="drive",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files = result.get("files", [])
            self._file_id = files[0]["id"] if files else None
            return self._file_id
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CONFIG] Drive lookup failed: {e}")
            return None

    def read(self) -> Optional[Dict[str, Any]]:
        file_id = self._find_file()
        if not file_id:
            return None
        try:
            raw = self._drive().drive.files().get_media(
                fileId=file_id, supportsAllDrives=True
            ).execute()
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            logger.info(f"[CONFIG] Loaded settings from Drive ({file_id})")
            return json.loads(text)
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CONFIG] Could not read settings from Drive: {e}")
            return None

    def write(self, data: Dict[str, Any]) -> bool:
        from googleapiclient.http import MediaIoBaseUpload

        payload = json.dumps(data, indent=2).encode("utf-8")
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False
        )
        try:
            with self._lock:
                file_id = self._find_file()
                if file_id:
                    self._drive().drive.files().update(
                        fileId=file_id, media_body=media, supportsAllDrives=True
                    ).execute()
                else:
                    created = self._drive().drive.files().create(
                        body={"name": CONFIG_FILENAME, "parents": [self._folder_id()]},
                        media_body=media,
                        fields="id",
                        supportsAllDrives=True,
                    ).execute()
                    self._file_id = created.get("id")
            logger.info(f"[CONFIG] Saved settings to Drive ({self._file_id})")
            return True
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CONFIG] Could not write settings to Drive: {e}")
            return False

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": "drive",
            "durable": True,
            "path": f"Drive / {CONFIG_FILENAME}",
            "exists": bool(self._find_file()),
        }


class PostgresConfigStore(ConfigStore):
    """
    A shared Postgres database, via DATABASE_URL.

    SAFETY: this database is shared with other services, so this class is
    deliberately narrow. It only ever touches ONE table, `publish_settings`,
    which it creates with IF NOT EXISTS. There is no DROP, no ALTER, no DELETE,
    no migration of anything, and it never reads or writes another table. If the
    table already exists with the expected shape it is left exactly as-is.

    One row, keyed 'default', holding the settings JSON.
    """

    name = "postgres"
    TABLE = "publish_settings"
    ROW_ID = "default"

    def __init__(self, url: str):
        self.url = self._normalise(url)
        self._lock = threading.Lock()
        self._ready = False

    @staticmethod
    def _normalise(url: str) -> str:
        # Render (and Heroku) hand out postgres://, which psycopg2 accepts but
        # some tooling doesn't; normalise so the URL works everywhere.
        if url.startswith("postgres://"):
            return "postgresql://" + url[len("postgres://"):]
        return url

    def _connect(self):
        import psycopg2
        return psycopg2.connect(self.url, connect_timeout=10)

    def _ensure_table(self, conn) -> None:
        if self._ready:
            return
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    id          TEXT PRIMARY KEY,
                    data        JSONB NOT NULL,
                    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        conn.commit()
        self._ready = True

    def read(self) -> Optional[Dict[str, Any]]:
        try:
            with self._connect() as conn:
                self._ensure_table(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"SELECT data FROM {self.TABLE} WHERE id = %s", (self.ROW_ID,)
                    )
                    row = cur.fetchone()
            if not row:
                return None
            data = row[0]
            # psycopg2 returns jsonb as a dict already; be tolerant either way.
            return data if isinstance(data, dict) else json.loads(data)
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CONFIG] Postgres read failed: {e}")
            return None

    def write(self, data: Dict[str, Any]) -> bool:
        try:
            # Cast the JSON text to jsonb in SQL rather than importing
            # psycopg2.extras — keeps the driver detail in _connect() alone.
            with self._lock, self._connect() as conn:
                self._ensure_table(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {self.TABLE} (id, data, updated_at)
                        VALUES (%s, %s::jsonb, NOW())
                        ON CONFLICT (id) DO UPDATE
                            SET data = EXCLUDED.data, updated_at = NOW()
                        """,
                        (self.ROW_ID, json.dumps(data)),
                    )
                conn.commit()
            logger.info("[CONFIG] Saved settings to Postgres")
            return True
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CONFIG] Postgres write failed: {e}")
            return False

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": "postgres",
            "durable": True,
            "path": f"{self.TABLE} (shared database)",
            "exists": self.read() is not None,
        }


class RedisConfigStore(ConfigStore):
    """Shared and fast. Loses data if the instance is wiped, so second choice."""

    name = "redis"
    KEY = "publish:config"

    def __init__(self, url: str):
        from redis import Redis
        self._redis = Redis.from_url(url)

    def read(self) -> Optional[Dict[str, Any]]:
        try:
            raw = self._redis.get(self.KEY)
            return json.loads(raw) if raw else None
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CONFIG] Redis read failed: {e}")
            return None

    def write(self, data: Dict[str, Any]) -> bool:
        try:
            self._redis.set(self.KEY, json.dumps(data))
            return True
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CONFIG] Redis write failed: {e}")
            return False

    def describe(self) -> Dict[str, Any]:
        return {"backend": "redis", "durable": True, "path": self.KEY, "exists": True}


class CachingStore(ConfigStore):
    """
    Wraps a backend with a short in-memory cache.

    Settings are read on nearly every request and written rarely, so this keeps
    Drive round trips off the hot path without making saves feel stale.
    """

    def __init__(self, inner: ConfigStore):
        self.inner = inner
        self.name = inner.name
        self._value: Optional[Dict[str, Any]] = None
        self._read_at = 0.0

    def read(self) -> Optional[Dict[str, Any]]:
        if self._value is not None and (time.time() - self._read_at) < _CACHE_SECONDS:
            return self._value
        value = self.inner.read()
        self._value = value
        self._read_at = time.time()
        return value

    def write(self, data: Dict[str, Any]) -> bool:
        ok = self.inner.write(data)
        if ok:
            self._value = data
            self._read_at = time.time()
        return ok

    def invalidate(self) -> None:
        self._value = None
        self._read_at = 0.0

    def describe(self) -> Dict[str, Any]:
        return self.inner.describe()


_store: Optional[CachingStore] = None


def _build_store() -> ConfigStore:
    choice = (os.getenv("PUBLISH_CONFIG_STORE") or "").strip().lower()
    local_path = os.getenv("PUBLISH_CONFIG_PATH") or os.path.join(
        os.path.dirname(os.getenv("UPLOAD_JOBS_DB", "data/upload_jobs.db")) or ".",
        "publish_classes.json",
    )

    if choice == "file":
        return FileConfigStore(local_path)
    if choice == "redis":
        return RedisConfigStore(os.environ["REDIS_URL"])
    if choice == "drive":
        return DriveConfigStore()
    if choice == "postgres":
        return PostgresConfigStore(os.environ["DATABASE_URL"])

    # Auto, most durable first.
    if os.getenv("DATABASE_URL"):
        try:
            return PostgresConfigStore(os.environ["DATABASE_URL"])
        except Exception as e:                      # noqa: BLE001
            logger.warning(f"[CONFIG] Postgres unavailable ({e}); falling back")
    if os.getenv("GOOGLE_CLIENT_EMAIL") or os.path.exists(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    ):
        return DriveConfigStore()
    if os.getenv("REDIS_URL"):
        try:
            return RedisConfigStore(os.environ["REDIS_URL"])
        except Exception as e:                      # noqa: BLE001
            logger.warning(f"[CONFIG] Redis unavailable ({e}); using local file")
    return FileConfigStore(local_path)


def get_config_store() -> CachingStore:
    global _store
    if _store is None:
        inner = _build_store()
        logger.info(f"[CONFIG] Settings stored via '{inner.name}'")
        _store = CachingStore(inner)
    return _store


def reset_store() -> None:
    """Drop the cached store — used by tests."""
    global _store
    _store = None
