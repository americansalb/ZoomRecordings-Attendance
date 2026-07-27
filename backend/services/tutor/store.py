"""
SQLite-backed persistence for the Live Tutor.

Mirrors the conventions in services/job_store.py: a single SQLite file on the
web container's local disk (persisted across deploys via the Render disk mounted
at backend/data), guarded by a process-level lock, with JSON columns for
structured fields.

Tables:
  - tutor_settings    single-row global config (capabilities, guardrails, bot)
  - tutor_reminders   admin-authored reminder/policy-reason templates
  - tutor_policies    rules that steer the AI when it drafts answers
  - tutor_sessions    bot presence per live meeting (summon/dismiss lifecycle)
  - tutor_approvals   the approval queue -- AI drafts awaiting a human decision
  - tutor_messages    append-only log of every inbound/outbound chat message

Nothing in this module sends anything; it only records state. The service layer
is responsible for the actual send + approval transitions.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now() -> float:
    return time.time()


# Default settings seeded on first run. "answer_questions" is intentionally off
# by default -- it is the most distracting capability and is opt-in/experimental.
DEFAULT_SETTINGS: Dict[str, Any] = {
    "capabilities": {
        "reminders": True,
        "answer_questions": False,  # experimental -- enable per the admin's test
        "direct_messages": True,
        "summon_dismiss": True,
    },
    # Only "approve_all" is wired today. Stored so the mode is explicit and the
    # UI can later offer looser modes without a schema change.
    "autonomy": "approve_all",
    "guardrails": {
        "min_seconds_between_messages": 45,
        "max_ai_messages_per_session": 20,
        "quiet_mode": False,
    },
    # Periodic per-student video snapshots. Off by default -- capturing student
    # faces is a consent/privacy decision the admin must opt into.
    "capture": {
        "enabled": False,
        "interval_seconds": 300,   # snapshot cadence per student
        "store_images": True,      # False = record presence flags only, discard pixels
    },
    "bot": {
        "display_name": "AALB Assistant",
        "announce_on_join": True,
        "announcement": (
            "Hi everyone -- I'm the AALB assistant. I'll post occasional "
            "reminders and can help answer questions in line with class "
            "policies. A staff member reviews anything I send."
        ),
    },
}


# Approval queue states.
APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"   # approved, queued to send
APPROVAL_REJECTED = "rejected"
APPROVAL_SENT = "sent"
APPROVAL_FAILED = "failed"
APPROVAL_TERMINAL = (APPROVAL_REJECTED, APPROVAL_SENT, APPROVAL_FAILED)

# Bot session lifecycle states.
SESSION_REQUESTED = "requested"   # admin asked the bot to join
SESSION_JOINING = "joining"
SESSION_IN_MEETING = "in_meeting"
SESSION_LEAVING = "leaving"
SESSION_LEFT = "left"
SESSION_ERROR = "error"
SESSION_ACTIVE_STATES = (SESSION_REQUESTED, SESSION_JOINING, SESSION_IN_MEETING, SESSION_LEAVING)


class TutorStore:
    """SQLite store for the Live Tutor. Single-process safe via a lock."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tutor_settings (
        id         INTEGER PRIMARY KEY CHECK (id = 1),
        data       TEXT NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tutor_reminders (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        label        TEXT NOT NULL,
        message      TEXT NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tutor_policies (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        title        TEXT NOT NULL,
        content      TEXT NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   REAL NOT NULL,
        updated_at   REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tutor_sessions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id     TEXT NOT NULL,
        meeting_uuid   TEXT,
        topic          TEXT,
        session_code   TEXT,
        status         TEXT NOT NULL,
        runtime_id     TEXT,
        join_url       TEXT,
        overrides      TEXT,
        summoned_by    TEXT,
        error          TEXT,
        created_at     REAL NOT NULL,
        updated_at     REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tutor_sessions_status ON tutor_sessions(status);
    CREATE INDEX IF NOT EXISTS idx_tutor_sessions_meeting ON tutor_sessions(meeting_id);

    CREATE TABLE IF NOT EXISTS tutor_approvals (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id          INTEGER,
        meeting_id          TEXT,
        channel             TEXT NOT NULL,
        target_id           TEXT,
        target_name         TEXT,
        source              TEXT NOT NULL,
        reason              TEXT,
        draft_text          TEXT NOT NULL,
        final_text          TEXT,
        context             TEXT,
        confidence          TEXT,
        status              TEXT NOT NULL DEFAULT 'pending',
        decided_by          TEXT,
        decided_at          REAL,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tutor_approvals_status ON tutor_approvals(status);
    CREATE INDEX IF NOT EXISTS idx_tutor_approvals_session ON tutor_approvals(session_id);

    CREATE TABLE IF NOT EXISTS tutor_messages (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id        INTEGER,
        meeting_id        TEXT,
        direction         TEXT NOT NULL,
        channel           TEXT NOT NULL,
        participant_id    TEXT,
        participant_name  TEXT,
        text              TEXT NOT NULL,
        source            TEXT NOT NULL,
        reason            TEXT,
        approval_id       INTEGER,
        created_at        REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tutor_messages_session ON tutor_messages(session_id);
    CREATE INDEX IF NOT EXISTS idx_tutor_messages_meeting ON tutor_messages(meeting_id);
    CREATE INDEX IF NOT EXISTS idx_tutor_messages_created ON tutor_messages(created_at);

    CREATE TABLE IF NOT EXISTS tutor_screenshots (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id        INTEGER,
        meeting_id        TEXT,
        participant_id    TEXT,
        participant_name  TEXT,
        registrant_id     TEXT,
        captured_at       REAL NOT NULL,
        video_on          INTEGER NOT NULL DEFAULT 0,
        face_present      INTEGER NOT NULL DEFAULT 0,
        stored            INTEGER NOT NULL DEFAULT 0,
        image_url         TEXT,
        drive_file_id     TEXT,
        created_at        REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tutor_shots_session ON tutor_screenshots(session_id);
    CREATE INDEX IF NOT EXISTS idx_tutor_shots_captured ON tutor_screenshots(captured_at);

    CREATE TABLE IF NOT EXISTS tutor_attendance (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id          INTEGER,
        meeting_id          TEXT,
        participant_id      TEXT NOT NULL,
        participant_name    TEXT,
        registrant_id       TEXT,
        joined_at           REAL,
        left_at             REAL,
        present             INTEGER NOT NULL DEFAULT 0,
        video_on            INTEGER NOT NULL DEFAULT 0,
        video_on_seconds    INTEGER NOT NULL DEFAULT 0,
        observed_seconds    INTEGER NOT NULL DEFAULT 0,
        face_checks         INTEGER NOT NULL DEFAULT 0,
        face_present_checks INTEGER NOT NULL DEFAULT 0,
        first_seen_at       REAL NOT NULL,
        last_seen_at        REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tutor_att_session ON tutor_attendance(session_id);
    CREATE INDEX IF NOT EXISTS idx_tutor_att_meeting ON tutor_attendance(meeting_id);
    -- One row per participant per session: the bot reports a cumulative ledger
    -- every tick, so these are upserts, not an append-only sample stream.
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tutor_att_unique
        ON tutor_attendance(session_id, participant_id);
    """

    # Columns that hold JSON and should be (de)serialized transparently.
    _JSON_COLS = {
        "tutor_sessions": ("overrides",),
        "tutor_approvals": ("context",),
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()
        self._seed_settings()
        logger.info(f"[TUTOR] SQLite store at {db_path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------------- settings

    def _seed_settings(self) -> None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT id FROM tutor_settings WHERE id = 1").fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO tutor_settings (id, data, updated_at) VALUES (1, ?, ?)",
                    (json.dumps(DEFAULT_SETTINGS), _now()),
                )
                conn.commit()

    def get_settings(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM tutor_settings WHERE id = 1").fetchone()
        data = json.loads(row["data"]) if row else {}
        return _deep_merge(DEFAULT_SETTINGS, data)

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge a partial settings patch and persist."""
        current = self.get_settings()
        merged = _deep_merge(current, patch)
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE tutor_settings SET data = ?, updated_at = ? WHERE id = 1",
                (json.dumps(merged), _now()),
            )
            conn.commit()
        return merged

    # ---------------------------------------------------------------- reminders

    def list_reminders(self, include_disabled: bool = True) -> List[Dict[str, Any]]:
        q = "SELECT * FROM tutor_reminders"
        if not include_disabled:
            q += " WHERE enabled = 1"
        q += " ORDER BY label COLLATE NOCASE"
        with self._connect() as conn:
            return [_row(r) for r in conn.execute(q).fetchall()]

    def get_reminder(self, reminder_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM tutor_reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return _row(r) if r else None

    def create_reminder(self, label: str, message: str, enabled: bool = True) -> Dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tutor_reminders (label, message, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (label, message, 1 if enabled else 0, now, now),
            )
            conn.commit()
            rid = cur.lastrowid
        return self.get_reminder(rid)  # type: ignore[return-value]

    def update_reminder(self, reminder_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        fields = {k: v for k, v in fields.items() if k in ("label", "message", "enabled")}
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE tutor_reminders SET {cols}, updated_at = ? WHERE id = ?",
                    (*fields.values(), _now(), reminder_id),
                )
                conn.commit()
        return self.get_reminder(reminder_id)

    def delete_reminder(self, reminder_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM tutor_reminders WHERE id = ?", (reminder_id,))
            conn.commit()

    # ----------------------------------------------------------------- policies

    def list_policies(self, include_disabled: bool = True) -> List[Dict[str, Any]]:
        q = "SELECT * FROM tutor_policies"
        if not include_disabled:
            q += " WHERE enabled = 1"
        q += " ORDER BY title COLLATE NOCASE"
        with self._connect() as conn:
            return [_row(r) for r in conn.execute(q).fetchall()]

    def get_policy(self, policy_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM tutor_policies WHERE id = ?", (policy_id,)
            ).fetchone()
        return _row(r) if r else None

    def create_policy(self, title: str, content: str, enabled: bool = True) -> Dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tutor_policies (title, content, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (title, content, 1 if enabled else 0, now, now),
            )
            conn.commit()
            pid = cur.lastrowid
        return self.get_policy(pid)  # type: ignore[return-value]

    def update_policy(self, policy_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        fields = {k: v for k, v in fields.items() if k in ("title", "content", "enabled")}
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE tutor_policies SET {cols}, updated_at = ? WHERE id = ?",
                    (*fields.values(), _now(), policy_id),
                )
                conn.commit()
        return self.get_policy(policy_id)

    def delete_policy(self, policy_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM tutor_policies WHERE id = ?", (policy_id,))
            conn.commit()

    # ------------------------------------------------------------- bot sessions

    def create_session(
        self,
        meeting_id: str,
        *,
        meeting_uuid: Optional[str] = None,
        topic: Optional[str] = None,
        session_code: Optional[str] = None,
        join_url: Optional[str] = None,
        summoned_by: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        status: str = SESSION_REQUESTED,
    ) -> Dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tutor_sessions
                   (meeting_id, meeting_uuid, topic, session_code, status, join_url,
                    overrides, summoned_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    meeting_id, meeting_uuid, topic, session_code, status, join_url,
                    json.dumps(overrides) if overrides is not None else None,
                    summoned_by, now, now,
                ),
            )
            conn.commit()
            sid = cur.lastrowid
        return self.get_session(sid)  # type: ignore[return-value]

    def update_session(self, session_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = ("status", "runtime_id", "meeting_uuid", "topic", "session_code",
                   "join_url", "overrides", "error")
        fields = {k: v for k, v in fields.items() if k in allowed}
        if "overrides" in fields and fields["overrides"] is not None and not isinstance(fields["overrides"], str):
            fields["overrides"] = json.dumps(fields["overrides"])
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE tutor_sessions SET {cols}, updated_at = ? WHERE id = ?",
                    (*fields.values(), _now(), session_id),
                )
                conn.commit()
        return self.get_session(session_id)

    def get_session(self, session_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM tutor_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _row(r, self._JSON_COLS["tutor_sessions"]) if r else None

    def get_session_by_runtime(self, runtime_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM tutor_sessions WHERE runtime_id = ? ORDER BY id DESC LIMIT 1",
                (runtime_id,),
            ).fetchone()
        return _row(r, self._JSON_COLS["tutor_sessions"]) if r else None

    def get_active_session_for_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        placeholders = ",".join("?" * len(SESSION_ACTIVE_STATES))
        with self._connect() as conn:
            r = conn.execute(
                f"""SELECT * FROM tutor_sessions
                    WHERE meeting_id = ? AND status IN ({placeholders})
                    ORDER BY id DESC LIMIT 1""",
                (meeting_id, *SESSION_ACTIVE_STATES),
            ).fetchone()
        return _row(r, self._JSON_COLS["tutor_sessions"]) if r else None

    def list_active_sessions(self) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" * len(SESSION_ACTIVE_STATES))
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM tutor_sessions
                    WHERE status IN ({placeholders})
                    ORDER BY id DESC""",
                SESSION_ACTIVE_STATES,
            ).fetchall()
        return [_row(r, self._JSON_COLS["tutor_sessions"]) for r in rows]

    def list_recent_sessions(self, limit: int = 25) -> List[Dict[str, Any]]:
        """Active sessions plus recently finished or failed ones.

        A session that fails to join drops straight out of the active states,
        so a list filtered to those makes a failed summon indistinguishable
        from a normal dismissal: the row simply disappears on the next poll and
        the error text it carries is never seen by anyone.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tutor_sessions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row(r, self._JSON_COLS["tutor_sessions"]) for r in rows]

    # ---------------------------------------------------------- approval queue

    def create_approval(
        self,
        *,
        draft_text: str,
        channel: str,
        source: str,
        session_id: Optional[int] = None,
        meeting_id: Optional[str] = None,
        target_id: Optional[str] = None,
        target_name: Optional[str] = None,
        reason: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        confidence: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tutor_approvals
                   (session_id, meeting_id, channel, target_id, target_name, source,
                    reason, draft_text, context, confidence, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    session_id, meeting_id, channel, target_id, target_name, source,
                    reason, draft_text,
                    json.dumps(context) if context is not None else None,
                    confidence, now, now,
                ),
            )
            conn.commit()
            aid = cur.lastrowid
        return self.get_approval(aid)  # type: ignore[return-value]

    def get_approval(self, approval_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM tutor_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return _row(r, self._JSON_COLS["tutor_approvals"]) if r else None

    def list_approvals(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tutor_approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tutor_approvals ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row(r, self._JSON_COLS["tutor_approvals"]) for r in rows]

    def count_pending_approvals(self) -> int:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT COUNT(*) AS c FROM tutor_approvals WHERE status = 'pending'"
            ).fetchone()
        return int(r["c"]) if r else 0

    def update_approval(self, approval_id: int, **fields: Any) -> Optional[Dict[str, Any]]:
        allowed = ("status", "final_text", "decided_by", "decided_at", "draft_text")
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            with self._lock, self._connect() as conn:
                conn.execute(
                    f"UPDATE tutor_approvals SET {cols}, updated_at = ? WHERE id = ?",
                    (*fields.values(), _now(), approval_id),
                )
                conn.commit()
        return self.get_approval(approval_id)

    # -------------------------------------------------------------- message log

    def add_message(
        self,
        *,
        direction: str,
        channel: str,
        text: str,
        source: str,
        session_id: Optional[int] = None,
        meeting_id: Optional[str] = None,
        participant_id: Optional[str] = None,
        participant_name: Optional[str] = None,
        reason: Optional[str] = None,
        approval_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tutor_messages
                   (session_id, meeting_id, direction, channel, participant_id,
                    participant_name, text, source, reason, approval_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, meeting_id, direction, channel, participant_id,
                    participant_name, text, source, reason, approval_id, now,
                ),
            )
            conn.commit()
            mid = cur.lastrowid
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM tutor_messages WHERE id = ?", (mid,)).fetchone()
        return _row(r)

    def list_messages(
        self,
        *,
        session_id: Optional[int] = None,
        meeting_id: Optional[str] = None,
        channel: Optional[str] = None,
        limit: int = 300,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if meeting_id is not None:
            clauses.append("meeting_id = ?")
            params.append(meeting_id)
        if channel is not None:
            clauses.append("channel = ?")
            params.append(channel)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tutor_messages{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row(r) for r in rows]

    def count_ai_messages_in_session(self, session_id: int) -> int:
        """AI-sourced outbound messages already sent in a session (for the cap)."""
        with self._connect() as conn:
            r = conn.execute(
                """SELECT COUNT(*) AS c FROM tutor_messages
                   WHERE session_id = ? AND direction = 'outbound'
                     AND source IN ('ai_answer', 'ai_dm')""",
                (session_id,),
            ).fetchone()
        return int(r["c"]) if r else 0

    def last_outbound_message_at(self, session_id: int) -> Optional[float]:
        with self._connect() as conn:
            r = conn.execute(
                """SELECT MAX(created_at) AS t FROM tutor_messages
                   WHERE session_id = ? AND direction = 'outbound'""",
                (session_id,),
            ).fetchone()
        return r["t"] if r and r["t"] is not None else None

    # ------------------------------------------------------- screenshot manifest

    def add_screenshot(
        self,
        *,
        captured_at: float,
        video_on: bool,
        face_present: bool,
        session_id: Optional[int] = None,
        meeting_id: Optional[str] = None,
        participant_id: Optional[str] = None,
        participant_name: Optional[str] = None,
        registrant_id: Optional[str] = None,
        stored: bool = False,
        image_url: Optional[str] = None,
        drive_file_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO tutor_screenshots
                   (session_id, meeting_id, participant_id, participant_name, registrant_id,
                    captured_at, video_on, face_present, stored, image_url, drive_file_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id, meeting_id, participant_id, participant_name, registrant_id,
                    captured_at, 1 if video_on else 0, 1 if face_present else 0,
                    1 if stored else 0, image_url, drive_file_id, now,
                ),
            )
            conn.commit()
            shot_id = cur.lastrowid
        with self._connect() as conn:
            r = conn.execute("SELECT * FROM tutor_screenshots WHERE id = ?", (shot_id,)).fetchone()
        return _row(r)

    # ---------------------------------------------------------------- attendance

    def record_attendance(
        self,
        *,
        participant_id: str,
        observed_at: float,
        session_id: Optional[int] = None,
        meeting_id: Optional[str] = None,
        participant_name: Optional[str] = None,
        registrant_id: Optional[str] = None,
        joined_at: Optional[float] = None,
        left_at: Optional[float] = None,
        present: bool = False,
        video_on: bool = False,
        video_on_seconds: int = 0,
        observed_seconds: int = 0,
        face_checked: bool = False,
        face_present: bool = False,
    ) -> Dict[str, Any]:
        """Upsert one participant's attendance for one session.

        The bot reports a cumulative ledger (total camera seconds so far, not a
        delta), so durations are replaced rather than added. The face counters
        are the exception: each report carries a single point-in-time check, so
        those accumulate into a ratio the reviewer can read as "a face was
        visible in N of M sampled frames".
        """
        now = _now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                """SELECT id, face_checks, face_present_checks FROM tutor_attendance
                   WHERE participant_id = ?
                     AND (session_id IS ? OR session_id = ?)""",
                (participant_id, session_id, session_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE tutor_attendance SET
                         meeting_id = COALESCE(?, meeting_id),
                         participant_name = COALESCE(?, participant_name),
                         registrant_id = COALESCE(?, registrant_id),
                         joined_at = COALESCE(?, joined_at),
                         left_at = ?,
                         present = ?, video_on = ?,
                         video_on_seconds = ?, observed_seconds = ?,
                         face_checks = ?, face_present_checks = ?,
                         last_seen_at = ?
                       WHERE id = ?""",
                    (
                        meeting_id, participant_name, registrant_id, joined_at, left_at,
                        1 if present else 0, 1 if video_on else 0,
                        int(video_on_seconds), int(observed_seconds),
                        int(existing["face_checks"]) + (1 if face_checked else 0),
                        int(existing["face_present_checks"]) + (1 if face_present else 0),
                        now, existing["id"],
                    ),
                )
                row_id = existing["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO tutor_attendance
                       (session_id, meeting_id, participant_id, participant_name,
                        registrant_id, joined_at, left_at, present, video_on,
                        video_on_seconds, observed_seconds, face_checks,
                        face_present_checks, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id, meeting_id, participant_id, participant_name,
                        registrant_id, joined_at, left_at,
                        1 if present else 0, 1 if video_on else 0,
                        int(video_on_seconds), int(observed_seconds),
                        1 if face_checked else 0, 1 if face_present else 0,
                        observed_at, now,
                    ),
                )
                row_id = cur.lastrowid
            conn.commit()
        with self._connect() as conn:
            r = conn.execute(
                "SELECT * FROM tutor_attendance WHERE id = ?", (row_id,)
            ).fetchone()
        return _row(r)

    def list_attendance(
        self,
        *,
        session_id: Optional[int] = None,
        meeting_id: Optional[str] = None,
        participant_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if meeting_id is not None:
            clauses.append("meeting_id = ?")
            params.append(meeting_id)
        if participant_id is not None:
            clauses.append("participant_id = ?")
            params.append(participant_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM tutor_attendance{where}
                    ORDER BY participant_name COLLATE NOCASE, participant_id LIMIT ?""",
                params,
            ).fetchall()
        return [_row(r) for r in rows]

    # --------------------------------------------------------------- screenshots

    def list_screenshots(
        self,
        *,
        session_id: Optional[int] = None,
        meeting_id: Optional[str] = None,
        participant_id: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if meeting_id is not None:
            clauses.append("meeting_id = ?")
            params.append(meeting_id)
        if participant_id is not None:
            clauses.append("participant_id = ?")
            params.append(participant_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tutor_screenshots{where} ORDER BY captured_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_row(r) for r in rows]


# --------------------------------------------------------------------- helpers


def _row(row: Optional[sqlite3.Row], json_cols: tuple = ()) -> Dict[str, Any]:
    """Convert a sqlite Row to a dict, deserializing JSON columns."""
    d = dict(row) if row else {}
    for k in json_cols:
        v = d.get(k)
        if v:
            try:
                d[k] = json.loads(v)
            except (TypeError, ValueError):
                pass
    return d


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge patch into a copy of base (patch wins)."""
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ----------------------------------------------------------------- singleton

_store: Optional[TutorStore] = None


def get_tutor_store() -> TutorStore:
    global _store
    if _store is None:
        db_path = os.getenv("TUTOR_DB", "data/tutor.db")
        _store = TutorStore(db_path)
    return _store
