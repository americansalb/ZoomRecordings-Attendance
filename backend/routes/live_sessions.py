"""
Live Sessions API Routes

Endpoints for monitoring active Zoom sessions and trainer presence.
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Dict, List, Optional
from datetime import datetime
import logging

from services.live_monitor_service import get_live_monitor_service, LiveMonitorService
from services.notification_service import notification_service
from services.zoom_service import zoom_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/live", tags=["live-sessions"])

# Initialize the live monitor service
_monitor_service: Optional[LiveMonitorService] = None


def get_monitor() -> LiveMonitorService:
    """Get the live monitor service instance."""
    global _monitor_service
    if _monitor_service is None:
        _monitor_service = get_live_monitor_service(zoom_service)
    return _monitor_service


@router.get("/sessions")
async def get_active_sessions() -> Dict:
    """
    Get all currently active Zoom sessions.

    Returns:
        List of active sessions with participant details
    """
    try:
        monitor = get_monitor()
        sessions = await monitor.get_live_meetings()

        return {
            "success": True,
            "sessions": monitor.get_active_sessions_summary(),
            "total": len(sessions),
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"[LIVE API] Error fetching active sessions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/{meeting_id}")
async def get_session_details(meeting_id: str) -> Dict:
    """
    Get detailed information about a specific active session.

    Args:
        meeting_id: Zoom meeting ID

    Returns:
        Session details with all participants
    """
    try:
        monitor = get_monitor()

        # Refresh sessions first
        await monitor.get_live_meetings()

        # Get session summary
        sessions = monitor.get_active_sessions_summary()

        for session in sessions:
            if session["meeting_id"] == meeting_id:
                return {
                    "success": True,
                    "session": session
                }

        raise HTTPException(status_code=404, detail=f"Session {meeting_id} not found or not active")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LIVE API] Error fetching session {meeting_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/check-alerts")
async def check_trainer_alerts(background_tasks: BackgroundTasks) -> Dict:
    """
    Manually trigger a check for trainer absence alerts.

    This is also run automatically by the scheduler.

    Returns:
        List of alerts that were generated
    """
    try:
        monitor = get_monitor()

        # Refresh active sessions
        await monitor.get_live_meetings()

        # Check for alerts
        alerts = await monitor.check_trainer_absence()

        # Send notifications for any alerts
        for alert in alerts:
            background_tasks.add_task(
                notification_service.send_trainer_alert,
                alert
            )

        return {
            "success": True,
            "alerts": alerts,
            "total": len(alerts),
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"[LIVE API] Error checking alerts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test-alert")
async def send_test_alert() -> Dict:
    """
    Send a test alert email to verify notification configuration.

    Returns:
        Success status
    """
    try:
        test_alert = {
            "type": "test",
            "urgency": "warning",
            "session_code": "TEST",
            "scheduled_start": datetime.utcnow().isoformat(),
            "has_students": True,
            "student_count": 5,
            "message": "🧪 TEST ALERT: This is a test of the trainer alert system. No action required."
        }

        success = await notification_service.send_trainer_alert(test_alert)

        if success:
            return {
                "success": True,
                "message": "Test alert sent successfully"
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="Failed to send test alert. Check SMTP configuration."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LIVE API] Error sending test alert: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_live_stats() -> Dict:
    """
    Get summary statistics of current live sessions.

    Returns:
        Stats about active sessions, trainers, students
    """
    try:
        monitor = get_monitor()

        # Refresh sessions
        await monitor.get_live_meetings()

        sessions = monitor.get_active_sessions_summary()

        total_trainers = sum(s["trainer_count"] for s in sessions)
        total_students = sum(s["student_count"] for s in sessions)
        sessions_with_trainers = sum(1 for s in sessions if s["has_trainer"])
        sessions_without_trainers = len(sessions) - sessions_with_trainers

        return {
            "success": True,
            "stats": {
                "active_sessions": len(sessions),
                "sessions_with_trainers": sessions_with_trainers,
                "sessions_without_trainers": sessions_without_trainers,
                "total_trainers": total_trainers,
                "total_students": total_students
            },
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        logger.error(f"[LIVE API] Error fetching stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def get_notification_config() -> Dict:
    """
    Get current notification configuration (without sensitive data).

    Returns:
        Configuration status
    """
    return {
        "success": True,
        "config": {
            "smtp_configured": bool(notification_service.smtp_user and notification_service.smtp_password),
            "alert_recipients_count": len(notification_service.alert_recipients),
            "google_spaces_configured": bool(notification_service.google_spaces_webhook)
        }
    }
