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


@dataclass
class ScheduledSession:
    """Represents a scheduled Zoom meeting."""
    meeting_id: str
    topic: str
    host_id: str
    host_name: str
    start_time: datetime
    duration_minutes: int
    session_code: Optional[str]
    status: str  # waiting, started, finished


class LiveMonitorService:
    """
    Service for monitoring active Zoom sessions and detecting trainer absence.
    """

    def __init__(self, zoom_service):
        self.zoom_service = zoom_service
        self._active_sessions: Dict[str, LiveSession] = {}
        self._scheduled_sessions: List[ScheduledSession] = []
        self._alert_history: Dict[str, Dict[str, datetime]] = {}  # meeting_id -> {alert_type: sent_time}
        logger.info("LiveMonitorService initialized")

    async def get_live_meetings(self) -> List[LiveSession]:
        """
        Get all currently active Zoom meetings.

        Uses multiple strategies to find live meetings.
        """
        live_sessions = []
        seen_meeting_ids = set()

        # Strategy 1: Try Dashboard API first (requires Business/Education plan)
        try:
            dashboard_sessions = await self._get_live_from_dashboard()
            for session in dashboard_sessions:
                if session.meeting_id not in seen_meeting_ids:
                    live_sessions.append(session)
                    seen_meeting_ids.add(session.meeting_id)
                    self._active_sessions[session.meeting_id] = session
            logger.info(f"[LIVE] Dashboard API returned {len(dashboard_sessions)} meetings")
        except Exception as e:
            logger.warning(f"[LIVE] Dashboard API failed (may require Business plan): {e}")

        # Strategy 2: Check each user for live meetings
        try:
            user_sessions = await self._get_live_from_users()
            for session in user_sessions:
                if session.meeting_id not in seen_meeting_ids:
                    live_sessions.append(session)
                    seen_meeting_ids.add(session.meeting_id)
                    self._active_sessions[session.meeting_id] = session
            logger.info(f"[LIVE] User meetings API returned {len(user_sessions)} additional meetings")
        except Exception as e:
            logger.warning(f"[LIVE] User meetings check failed: {e}")

        logger.info(f"[LIVE] Total active meetings found: {len(live_sessions)}")
        return live_sessions

    async def _get_live_from_dashboard(self) -> List[LiveSession]:
        """Get live meetings from Dashboard API (Business/Education plans only)."""
        sessions = []

        for account in self.zoom_service.accounts.values():
            try:
                response = await self.zoom_service._make_request(
                    "GET",
                    "/metrics/meetings",
                    account,
                    params={"type": "live", "page_size": 100}
                )

                for meeting in response.get("meetings", []):
                    session = await self._create_live_session(meeting, account)
                    if session:
                        sessions.append(session)

            except Exception as e:
                logger.debug(f"[LIVE] Dashboard API failed for {account.name}: {e}")

        return sessions

    async def _get_live_from_users(self) -> List[LiveSession]:
        """Get live meetings by checking each user's meetings."""
        sessions = []

        for account in self.zoom_service.accounts.values():
            try:
                # Get all users in this account
                users = await self.zoom_service.list_users(account.account_id)

                for user in users:
                    try:
                        # Check if user has any live meetings
                        response = await self.zoom_service._make_request(
                            "GET",
                            f"/users/{user['id']}/meetings",
                            account,
                            params={"type": "live", "page_size": 30}
                        )

                        for meeting in response.get("meetings", []):
                            session = await self._create_live_session_from_user_meeting(meeting, user, account)
                            if session:
                                sessions.append(session)

                    except Exception as e:
                        # User might not have any live meetings
                        logger.debug(f"[LIVE] No live meetings for {user.get('email')}: {e}")

            except Exception as e:
                logger.warning(f"[LIVE] Failed to check users for {account.name}: {e}")

        return sessions

    async def get_scheduled_meetings(self, days_ahead: int = 7) -> List[ScheduledSession]:
        """
        Get all scheduled meetings for the calendar view.

        Args:
            days_ahead: Number of days ahead to fetch (default 7)
        """
        scheduled = []

        for account in self.zoom_service.accounts.values():
            try:
                users = await self.zoom_service.list_users(account.account_id)

                for user in users:
                    try:
                        # Get upcoming/scheduled meetings
                        response = await self.zoom_service._make_request(
                            "GET",
                            f"/users/{user['id']}/meetings",
                            account,
                            params={"type": "upcoming", "page_size": 100}
                        )

                        for meeting in response.get("meetings", []):
                            session = self._create_scheduled_session(meeting, user)
                            if session:
                                # Filter by date range
                                if session.start_time <= datetime.utcnow() + timedelta(days=days_ahead):
                                    scheduled.append(session)

                    except Exception as e:
                        logger.debug(f"[SCHEDULE] Error fetching for {user.get('email')}: {e}")

            except Exception as e:
                logger.warning(f"[SCHEDULE] Failed for {account.name}: {e}")

        # Sort by start time
        scheduled.sort(key=lambda s: s.start_time)
        self._scheduled_sessions = scheduled

        logger.info(f"[SCHEDULE] Found {len(scheduled)} scheduled meetings")
        return scheduled

    async def _create_live_session(self, meeting_data: Dict, account) -> Optional[LiveSession]:
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

            # Try to fetch participants
            session.participants = await self._get_meeting_participants(meeting_uuid or meeting_id, account)

            return session

        except Exception as e:
            logger.error(f"[LIVE] Error creating session from meeting data: {e}")
            return None

    async def _create_live_session_from_user_meeting(self, meeting_data: Dict, host_user: Dict, account) -> Optional[LiveSession]:
        """Create a LiveSession from user Meetings API data."""
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
                host_name=host_user.get("display_name", host_user.get("email", "")),
                start_time=start_time or datetime.utcnow(),
                scheduled_start=None,
                scheduled_duration_minutes=meeting_data.get("duration"),
                session_code=session_code,
                participants=[]
            )

            # Try to fetch participants (may fail without Dashboard API)
            session.participants = await self._get_meeting_participants(meeting_uuid or meeting_id, account)

            return session

        except Exception as e:
            logger.error(f"[LIVE] Error creating session: {e}")
            return None

    def _create_scheduled_session(self, meeting_data: Dict, host_user: Dict) -> Optional[ScheduledSession]:
        """Create a ScheduledSession from meetings API data."""
        try:
            meeting_id = str(meeting_data.get("id", ""))
            topic = meeting_data.get("topic", "")
            session_code = self._extract_session_code(topic)

            start_time = None
            if meeting_data.get("start_time"):
                start_time = datetime.fromisoformat(
                    meeting_data["start_time"].replace("Z", "+00:00")
                )

            if not start_time:
                return None

            # Determine status
            now = datetime.utcnow().replace(tzinfo=start_time.tzinfo) if start_time.tzinfo else datetime.utcnow()
            duration = meeting_data.get("duration", 60)
            end_time = start_time + timedelta(minutes=duration)

            if now < start_time:
                status = "waiting"
            elif now > end_time:
                status = "finished"
            else:
                status = "started"

            return ScheduledSession(
                meeting_id=meeting_id,
                topic=topic,
                host_id=host_user.get("id", ""),
                host_name=host_user.get("display_name", host_user.get("email", "")),
                start_time=start_time,
                duration_minutes=duration,
                session_code=session_code,
                status=status
            )

        except Exception as e:
            logger.error(f"[SCHEDULE] Error creating scheduled session: {e}")
            return None

    async def _get_meeting_participants(self, meeting_id: str, account) -> List[LiveParticipant]:
        """Get current participants in a live meeting."""
        participants = []

        try:
            # Try Dashboard API first (more detailed)
            response = await self.zoom_service._make_request(
                "GET",
                f"/metrics/meetings/{meeting_id}/participants",
                account,
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

    def get_scheduled_sessions_summary(self) -> List[Dict]:
        """Get summary of scheduled sessions for the calendar."""
        return [
            {
                "meeting_id": session.meeting_id,
                "topic": session.topic,
                "session_code": session.session_code,
                "start_time": session.start_time.isoformat() if session.start_time else None,
                "duration_minutes": session.duration_minutes,
                "host_name": session.host_name,
                "status": session.status
            }
            for session in self._scheduled_sessions
        ]


# Singleton instance (will be initialized with zoom_service)
live_monitor_service: Optional[LiveMonitorService] = None


def get_live_monitor_service(zoom_service) -> LiveMonitorService:
    """Get or create the live monitor service."""
    global live_monitor_service
    if live_monitor_service is None:
        live_monitor_service = LiveMonitorService(zoom_service)
    return live_monitor_service
