"""
Live Session Monitor Service

Monitors active Zoom meetings and tracks participant presence.
Detects when trainers (hosts/co-hosts) are missing from scheduled sessions.
"""

import os
import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ParticipantRole(Enum):
    HOST = "host"
    CO_HOST = "co-host"
    STUDENT = "student"  # Any non-host/co-host


@dataclass
class LiveParticipant:
    """Represents a participant currently in a meeting."""
    user_id: str
    name: str
    email: Optional[str]
    role: ParticipantRole
    join_time: datetime

    def is_trainer(self) -> bool:
        """Check if this participant is a trainer (host or co-host)."""
        return self.role in (ParticipantRole.HOST, ParticipantRole.CO_HOST)


@dataclass
class LiveSession:
    """Represents an active Zoom meeting session."""
    meeting_id: str
    meeting_uuid: str
    topic: str
    host_id: str
    host_name: str
    start_time: datetime
    scheduled_start: Optional[datetime]
    scheduled_duration_minutes: Optional[int]
    session_code: Optional[str]
    participants: List[LiveParticipant] = field(default_factory=list)

    @property
    def trainer_count(self) -> int:
        """Count of trainers (hosts/co-hosts) in the meeting."""
        return sum(1 for p in self.participants if p.is_trainer())

    @property
    def student_count(self) -> int:
        """Count of students (non-trainers) in the meeting."""
        return sum(1 for p in self.participants if not p.is_trainer())

    @property
    def has_trainer(self) -> bool:
        """Check if at least one trainer is present."""
        return self.trainer_count > 0

    @property
    def has_students_without_trainer(self) -> bool:
        """Check if students are present but no trainer."""
        return self.student_count > 0 and not self.has_trainer


class LiveMonitorService:
    """
    Service for monitoring active Zoom sessions and detecting trainer absence.
    """

    def __init__(self, zoom_service):
        self.zoom_service = zoom_service
        self._active_sessions: Dict[str, LiveSession] = {}
        self._alert_history: Dict[str, Dict[str, datetime]] = {}  # meeting_id -> {alert_type: sent_time}
        logger.info("LiveMonitorService initialized")

    async def get_live_meetings(self) -> List[LiveSession]:
        """
        Get all currently active Zoom meetings.

        Uses Zoom Dashboard API to get live meetings.
        """
        try:
            # Use Zoom's Dashboard Meetings API
            # GET /metrics/meetings?type=live
            response = await self.zoom_service.api_request(
                "GET",
                "/metrics/meetings",
                params={"type": "live", "page_size": 100}
            )

            meetings = response.get("meetings", [])
            logger.info(f"[LIVE] Found {len(meetings)} active meetings")

            live_sessions = []
            for meeting in meetings:
                session = await self._create_live_session(meeting)
                if session:
                    live_sessions.append(session)
                    self._active_sessions[session.meeting_id] = session

            return live_sessions

        except Exception as e:
            logger.error(f"[LIVE] Error fetching live meetings: {e}")
            # Fallback: try to get meetings from recordings that are in progress
            return await self._get_live_meetings_fallback()

    async def _get_live_meetings_fallback(self) -> List[LiveSession]:
        """
        Fallback method to detect live meetings when Dashboard API is unavailable.
        Uses the meetings list API to find meetings currently in progress.
        """
        try:
            # Get all users and check their ongoing meetings
            users = await self.zoom_service.list_users()
            live_sessions = []

            for user in users:
                try:
                    # Check if user has any live meetings
                    response = await self.zoom_service.api_request(
                        "GET",
                        f"/users/{user['id']}/meetings",
                        params={"type": "live", "page_size": 30}
                    )

                    for meeting in response.get("meetings", []):
                        session = await self._create_live_session_from_meeting(meeting, user)
                        if session:
                            live_sessions.append(session)
                            self._active_sessions[session.meeting_id] = session

                except Exception as e:
                    logger.warning(f"[LIVE] Error checking live meetings for user {user.get('email')}: {e}")

            return live_sessions

        except Exception as e:
            logger.error(f"[LIVE] Fallback method failed: {e}")
            return []

    async def _create_live_session(self, meeting_data: Dict) -> Optional[LiveSession]:
        """Create a LiveSession from Dashboard API meeting data."""
        try:
            meeting_id = str(meeting_data.get("id", ""))
            meeting_uuid = meeting_data.get("uuid", "")

            # Extract session code from topic
            topic = meeting_data.get("topic", "")
            session_code = self._extract_session_code(topic)

            # Parse times
            start_time = None
            if meeting_data.get("start_time"):
                start_time = datetime.fromisoformat(
                    meeting_data["start_time"].replace("Z", "+00:00")
                )

            session = LiveSession(
                meeting_id=meeting_id,
                meeting_uuid=meeting_uuid,
                topic=topic,
                host_id=meeting_data.get("host_id", ""),
                host_name=meeting_data.get("host", ""),
                start_time=start_time or datetime.utcnow(),
                scheduled_start=None,  # Will be filled from schedule
                scheduled_duration_minutes=meeting_data.get("duration"),
                session_code=session_code,
                participants=[]
            )

            # Fetch participants
            session.participants = await self._get_meeting_participants(meeting_uuid or meeting_id)

            return session

        except Exception as e:
            logger.error(f"[LIVE] Error creating session from meeting data: {e}")
            return None

    async def _create_live_session_from_meeting(self, meeting_data: Dict, host_user: Dict) -> Optional[LiveSession]:
        """Create a LiveSession from Meetings API data."""
        try:
            meeting_id = str(meeting_data.get("id", ""))
            meeting_uuid = meeting_data.get("uuid", "")

            topic = meeting_data.get("topic", "")
            session_code = self._extract_session_code(topic)

            start_time = None
            if meeting_data.get("start_time"):
                start_time = datetime.fromisoformat(
                    meeting_data["start_time"].replace("Z", "+00:00")
                )

            session = LiveSession(
                meeting_id=meeting_id,
                meeting_uuid=meeting_uuid,
                topic=topic,
                host_id=host_user.get("id", ""),
                host_name=host_user.get("first_name", "") + " " + host_user.get("last_name", ""),
                start_time=start_time or datetime.utcnow(),
                scheduled_start=None,
                scheduled_duration_minutes=meeting_data.get("duration"),
                session_code=session_code,
                participants=[]
            )

            # Fetch participants
            session.participants = await self._get_meeting_participants(meeting_uuid or meeting_id)

            return session

        except Exception as e:
            logger.error(f"[LIVE] Error creating session: {e}")
            return None

    async def _get_meeting_participants(self, meeting_id: str) -> List[LiveParticipant]:
        """Get current participants in a live meeting."""
        participants = []

        try:
            # Try Dashboard API first (more detailed)
            response = await self.zoom_service.api_request(
                "GET",
                f"/metrics/meetings/{meeting_id}/participants",
                params={"type": "live", "page_size": 100}
            )

            for p in response.get("participants", []):
                role = self._determine_role(p)

                join_time = datetime.utcnow()
                if p.get("join_time"):
                    join_time = datetime.fromisoformat(
                        p["join_time"].replace("Z", "+00:00")
                    )

                participants.append(LiveParticipant(
                    user_id=p.get("user_id", p.get("id", "")),
                    name=p.get("user_name", p.get("name", "Unknown")),
                    email=p.get("email"),
                    role=role,
                    join_time=join_time
                ))

        except Exception as e:
            logger.warning(f"[LIVE] Could not fetch participants for {meeting_id}: {e}")

        return participants

    def _determine_role(self, participant_data: Dict) -> ParticipantRole:
        """Determine participant role from Zoom data."""
        # Check various fields that indicate role
        role_str = str(participant_data.get("role", "")).lower()
        user_type = participant_data.get("user_type", 0)

        if role_str == "host" or user_type == 1:
            return ParticipantRole.HOST
        elif role_str == "co-host":
            return ParticipantRole.CO_HOST
        else:
            return ParticipantRole.STUDENT

    def _extract_session_code(self, topic: str) -> Optional[str]:
        """Extract session code from meeting topic."""
        import re

        # Try patterns like "Session 127", "Sess 127", "S127"
        patterns = [
            r'[Ss]ession\s*(\d+)',
            r'[Ss]ess\s*(\d+)',
            r'\b[Ss](\d{2,3})\b',
        ]

        for pattern in patterns:
            match = re.search(pattern, topic)
            if match:
                return match.group(1)

        return None

    async def check_trainer_absence(self, schedule_service=None) -> List[Dict]:
        """
        Check all scheduled sessions for trainer absence.

        Returns list of alerts that should be sent.
        """
        alerts = []
        now = datetime.utcnow()

        # Get scheduled sessions for today
        scheduled_sessions = await self._get_todays_schedule(schedule_service)

        for scheduled in scheduled_sessions:
            session_code = scheduled.get("session_code")
            scheduled_start = scheduled.get("scheduled_start")

            if not scheduled_start:
                continue

            # Find if this session is live
            live_session = self._find_live_session(session_code)

            # Calculate time difference
            time_diff = (scheduled_start - now).total_seconds() / 60  # in minutes

            # Check alert conditions
            alert = self._check_alert_conditions(
                session_code=session_code,
                scheduled_start=scheduled_start,
                live_session=live_session,
                time_diff_minutes=time_diff
            )

            if alert:
                alerts.append(alert)

        return alerts

    def _check_alert_conditions(
        self,
        session_code: str,
        scheduled_start: datetime,
        live_session: Optional[LiveSession],
        time_diff_minutes: float
    ) -> Optional[Dict]:
        """
        Check if any alert condition is met.

        Alert conditions:
        1. 5 minutes before scheduled start - no trainer
        2. 2 minutes before scheduled start - no trainer
        3. 5 minutes after scheduled start - no trainer (CRITICAL)
        """
        alert_key = f"{session_code}:{scheduled_start.isoformat()}"

        # Determine if trainer is present
        has_trainer = live_session and live_session.has_trainer if live_session else False
        has_students = live_session and live_session.student_count > 0 if live_session else False

        # Already has trainer - no alert needed
        if has_trainer:
            return None

        alert_type = None
        urgency = None

        # 5 minutes before (-5 to -4 minutes window)
        if -5 <= time_diff_minutes < -4:
            alert_type = "5_min_before"
            urgency = "warning"
        # 2 minutes before (-2 to -1 minutes window)
        elif -2 <= time_diff_minutes < -1:
            alert_type = "2_min_before"
            urgency = "urgent"
        # 5 minutes after (5 to 6 minutes window)
        elif 5 <= time_diff_minutes < 6:
            alert_type = "5_min_after"
            urgency = "critical"

        if not alert_type:
            return None

        # Check if we already sent this alert
        if alert_key in self._alert_history:
            if alert_type in self._alert_history[alert_key]:
                return None  # Already sent

        # Record that we're sending this alert
        if alert_key not in self._alert_history:
            self._alert_history[alert_key] = {}
        self._alert_history[alert_key][alert_type] = datetime.utcnow()

        return {
            "type": alert_type,
            "urgency": urgency,
            "session_code": session_code,
            "scheduled_start": scheduled_start.isoformat(),
            "has_students": has_students,
            "student_count": live_session.student_count if live_session else 0,
            "message": self._format_alert_message(
                alert_type, urgency, session_code, scheduled_start, has_students,
                live_session.student_count if live_session else 0
            )
        }

    def _format_alert_message(
        self,
        alert_type: str,
        urgency: str,
        session_code: str,
        scheduled_start: datetime,
        has_students: bool,
        student_count: int
    ) -> str:
        """Format the alert message."""
        time_str = scheduled_start.strftime("%I:%M %p")

        if alert_type == "5_min_before":
            msg = f"⚠️ WARNING: Session {session_code} starts at {time_str} - No trainer has joined yet."
        elif alert_type == "2_min_before":
            msg = f"🚨 URGENT: Session {session_code} starts in 2 minutes - Still no trainer!"
        else:  # 5_min_after
            msg = f"🔴 CRITICAL: Session {session_code} started 5 minutes ago - NO TRAINER PRESENT!"

        if has_students:
            msg += f"\n{student_count} student(s) are waiting without supervision!"

        return msg

    def _find_live_session(self, session_code: str) -> Optional[LiveSession]:
        """Find a live session by session code."""
        for session in self._active_sessions.values():
            if session.session_code == session_code:
                return session
        return None

    async def _get_todays_schedule(self, schedule_service=None) -> List[Dict]:
        """Get today's scheduled sessions."""
        # This would integrate with your schedule spreadsheet
        # For now, return empty list - will be implemented with schedule integration
        return []

    def get_active_sessions_summary(self) -> List[Dict]:
        """Get summary of all active sessions for the dashboard."""
        return [
            {
                "meeting_id": session.meeting_id,
                "topic": session.topic,
                "session_code": session.session_code,
                "start_time": session.start_time.isoformat() if session.start_time else None,
                "host_name": session.host_name,
                "trainer_count": session.trainer_count,
                "student_count": session.student_count,
                "has_trainer": session.has_trainer,
                "participants": [
                    {
                        "name": p.name,
                        "email": p.email,
                        "role": p.role.value,
                        "is_trainer": p.is_trainer(),
                        "join_time": p.join_time.isoformat() if p.join_time else None
                    }
                    for p in session.participants
                ]
            }
            for session in self._active_sessions.values()
        ]


# Singleton instance (will be initialized with zoom_service)
live_monitor_service: Optional[LiveMonitorService] = None


def get_live_monitor_service(zoom_service) -> LiveMonitorService:
    """Get or create the live monitor service."""
    global live_monitor_service
    if live_monitor_service is None:
        live_monitor_service = LiveMonitorService(zoom_service)
    return live_monitor_service
