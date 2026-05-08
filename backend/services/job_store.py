"""
Job store abstraction for upload jobs.

Two backends:
  - SQLiteJobStore: durable on a single web container's local disk.
    Survives uvicorn-internal restarts but does NOT share state with a
    separate worker container (each Render service has its own filesystem).
  - RedisJobStore: shared between web and a separate RQ worker container.

Selected by environment:
  - If REDIS_URL is set -> RedisJobStore (and RQ-backed queueing in routes).
  - Otherwise -> SQLiteJobStore (and in-process BackgroundTasks).

Both implementations expose mark_stale_failed(), which the FastAPI startup
hook calls so that any job left in an in-progress state by a previous
process appears as 'failed' to the frontend instead of returning 404
forever.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


IN_PROGRESS_STATUSES = ("pending", "downloading", "trimming", "uploading")
TERMINAL_STATUSES = ("completed", "failed")


class JobStore(ABC):
    """Abstract job state store."""

    @abstractmethod
    def create_job(self, job_id: str, request_data: Dict[str, Any]) -> None: ...

    @abstractmethod
    def update_job(self, job_id: str, **fields: Any) -> None: ...

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]: ...

    @abstractmethod
    def list_jobs(self, limit: int = 100) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def mark_stale_failed(self) -> int:
        """Mark any in-progress jobs as failed. Returns number marked."""


def _now() -> float:
    return time.time()


class SQLiteJobStore(JobStore):
    """SQLite-backed job store. Single-process safe via a lock."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS upload_jobs (
        job_id       TEXT PRIMARY KEY,
        status       TEXT NOT NULL,
        progress     REAL NOT NULL DEFAULT 0,
        message      TEXT NOT NULL DEFAULT '',
        request_data TEXT NOT NULL,
        result       TEXT,
        error        TEXT,
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_upload_jobs_status ON upload_jobs(status);
    CREATE INDEX IF NOT EXISTS idx_upload_jobs_created_at ON upload_jobs(created_at);
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()
        logger.info(f"[JOBSTORE] SQLite store at {db_path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def create_job(self, job_id: str, request_data: Dict[str, Any]) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO upload_jobs
                   (job_id, status, progress, message, request_data, created_at, updated_at)
                   VALUES (?, 'pending', 0, 'Job queued', ?, ?, ?)""",
                (job_id, json.dumps(request_data), now, now),
            )
            conn.commit()

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        # Serialize dict-typed fields
        for k in ("result", "request_data"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])

        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [_now(), job_id]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE upload_jobs SET {cols}, updated_at = ? WHERE job_id = ?",
                values,
            )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM upload_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM upload_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def mark_stale_failed(self) -> int:
        placeholders = ",".join("?" * len(IN_PROGRESS_STATUSES))
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"""UPDATE upload_jobs
                    SET status = 'failed',
                        error = 'Server restarted before job completed. Please retry.',
                        message = 'Failed: server restarted',
                        updated_at = ?
                    WHERE status IN ({placeholders})""",
                (now, *IN_PROGRESS_STATUSES),
            )
            conn.commit()
            return cur.rowcount


class RedisJobStore(JobStore):
    """Redis-backed job store. Shared between web and worker."""

    KEY_PREFIX = "upload_job:"
    INDEX_KEY = "upload_jobs:index"  # sorted set: created_at -> job_id

    def __init__(self, redis_url: str):
        # Imported lazily so the SQLite path doesn't require the redis package.
        import redis  # type: ignore

        self.redis = redis.Redis.from_url(redis_url, decode_responses=True)
        # Probe; raises if unreachable.
        self.redis.ping()
        logger.info("[JOBSTORE] Redis store connected")

    def _key(self, job_id: str) -> str:
        return f"{self.KEY_PREFIX}{job_id}"

    def create_job(self, job_id: str, request_data: Dict[str, Any]) -> None:
        now = _now()
        record = {
            "job_id": job_id,
            "status": "pending",
            "progress": 0.0,
            "message": "Job queued",
            "request_data": request_data,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        pipe = self.redis.pipeline()
        pipe.set(self._key(job_id), json.dumps(record))
        pipe.zadd(self.INDEX_KEY, {job_id: now})
        pipe.execute()

    def update_job(self, job_id: str, **fields: Any) -> None:
        key = self._key(job_id)
        raw = self.redis.get(key)
        if not raw:
            return
        record = json.loads(raw)
        record.update(fields)
        record["updated_at"] = _now()
        self.redis.set(key, json.dumps(record))

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        raw = self.redis.get(self._key(job_id))
        return json.loads(raw) if raw else None

    def list_jobs(self, limit: int = 100) -> List[Dict[str, Any]]:
        ids = self.redis.zrevrange(self.INDEX_KEY, 0, limit - 1)
        if not ids:
            return []
        raw_records = self.redis.mget([self._key(i) for i in ids])
        return [json.loads(r) for r in raw_records if r]

    def mark_stale_failed(self) -> int:
        # Web container is the only one that calls this on startup. With a
        # separate worker, calling this from web would also kill the worker's
        # in-flight jobs. So we look at created_at and only mark old ones.
        cutoff = _now() - 60 * 60  # anything in-progress for >1h is stale
        ids = self.redis.zrangebyscore(self.INDEX_KEY, "-inf", cutoff)
        marked = 0
        for job_id in ids:
            key = self._key(job_id)
            raw = self.redis.get(key)
            if not raw:
                continue
            record = json.loads(raw)
            if record.get("status") in IN_PROGRESS_STATUSES:
                record["status"] = "failed"
                record["error"] = "Server restarted before job completed. Please retry."
                record["message"] = "Failed: server restarted"
                record["updated_at"] = _now()
                self.redis.set(key, json.dumps(record))
                marked += 1
        return marked


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for k in ("result", "request_data"):
        v = d.get(k)
        if v:
            try:
                d[k] = json.loads(v)
            except (TypeError, ValueError):
                pass
    return d


_singleton: Optional[JobStore] = None


def get_job_store() -> JobStore:
    """Return the configured job store (Redis if REDIS_URL, else SQLite)."""
    global _singleton
    if _singleton is not None:
        return _singleton

    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            _singleton = RedisJobStore(redis_url)
            return _singleton
        except Exception as e:
            logger.error(f"[JOBSTORE] Redis init failed ({e}); falling back to SQLite")

    db_path = os.getenv("UPLOAD_JOBS_DB", "data/upload_jobs.db")
    _singleton = SQLiteJobStore(db_path)
    return _singleton


def using_redis() -> bool:
    """Whether the job store is Redis-backed (and queue should use RQ)."""
    return isinstance(get_job_store(), RedisJobStore)
