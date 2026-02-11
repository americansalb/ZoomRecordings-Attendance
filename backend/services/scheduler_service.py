"""
Scheduler Service

Background task scheduler for periodic trainer absence checks.
Uses APScheduler for reliable task scheduling.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class SchedulerService:
    """Background scheduler for periodic tasks."""

    def __init__(self):
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._is_running = False
        self._check_interval_seconds = 60  # Check every minute

    def start(self):
        """Start the background scheduler."""
        if self._is_running:
            logger.warning("[SCHEDULER] Scheduler already running")
            return

        self.scheduler = AsyncIOScheduler()

        # Add the trainer absence check job
        self.scheduler.add_job(
            self._check_trainer_absence,
            trigger=IntervalTrigger(seconds=self._check_interval_seconds),
            id='trainer_absence_check',
            name='Check for trainer absence in active sessions',
            replace_existing=True
        )

        self.scheduler.start()
        self._is_running = True
        logger.info(f"[SCHEDULER] Started with {self._check_interval_seconds}s interval")

    def stop(self):
        """Stop the background scheduler."""
        if self.scheduler and self._is_running:
            self.scheduler.shutdown(wait=False)
            self._is_running = False
            logger.info("[SCHEDULER] Stopped")

    async def _check_trainer_absence(self):
        """
        Check for trainer absence in active sessions.
        This runs periodically and sends alerts as needed.
        """
        try:
            logger.debug(f"[SCHEDULER] Running trainer absence check at {datetime.utcnow().isoformat()}")

            # Import here to avoid circular imports
            from services.live_monitor_service import get_live_monitor_service
            from services.notification_service import notification_service
            from services.zoom_service import zoom_service

            # Get the monitor service
            monitor = get_live_monitor_service(zoom_service)

            # Refresh active sessions
            await monitor.get_live_meetings()

            # Check for alerts
            alerts = await monitor.check_trainer_absence()

            if alerts:
                logger.info(f"[SCHEDULER] Found {len(alerts)} trainer absence alerts")

                # Send notifications for each alert
                for alert in alerts:
                    try:
                        success = await notification_service.send_trainer_alert(alert)
                        if success:
                            logger.info(f"[SCHEDULER] Alert sent for session {alert.get('session_code', 'unknown')}")
                        else:
                            logger.warning(f"[SCHEDULER] Failed to send alert for session {alert.get('session_code', 'unknown')}")
                    except Exception as e:
                        logger.error(f"[SCHEDULER] Error sending alert: {e}")
            else:
                logger.debug("[SCHEDULER] No trainer absence alerts")

        except Exception as e:
            logger.error(f"[SCHEDULER] Error in trainer absence check: {e}")


# Global scheduler instance
scheduler_service = SchedulerService()
