"""
Notification Service

Handles sending alerts via email (and future: SMS, Google Spaces).
"""

import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional, Dict
from datetime import datetime

logger = logging.getLogger(__name__)


class NotificationService:
    """
    Service for sending notifications via various channels.

    Currently supports:
    - Email (SMTP)

    Future:
    - SMS (Twilio)
    - Google Spaces (Webhook)
    """

    def __init__(self):
        # Email configuration
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.from_email = os.getenv("NOTIFICATION_FROM_EMAIL", self.smtp_user)

        # Recipients for trainer alerts
        self.alert_recipients = self._parse_recipients(
            os.getenv("TRAINER_ALERT_EMAILS", "")
        )

        # Google Spaces webhook (future)
        self.google_spaces_webhook = os.getenv("GOOGLE_SPACES_WEBHOOK", "")

        logger.info(f"NotificationService initialized. Recipients: {len(self.alert_recipients)}")

    def _parse_recipients(self, recipients_str: str) -> List[str]:
        """Parse comma-separated email list."""
        if not recipients_str:
            return []
        return [email.strip() for email in recipients_str.split(",") if email.strip()]

    async def send_trainer_alert(self, alert: Dict) -> bool:
        """
        Send a trainer absence alert to all configured recipients.

        Args:
            alert: Alert data from LiveMonitorService

        Returns:
            True if sent successfully
        """
        if not self.alert_recipients:
            logger.warning("[NOTIFY] No alert recipients configured. Set TRAINER_ALERT_EMAILS env var.")
            return False

        urgency = alert.get("urgency", "warning")
        session_code = alert.get("session_code", "Unknown")
        message = alert.get("message", "Trainer alert")

        # Create email subject based on urgency
        if urgency == "critical":
            subject = f"🔴 CRITICAL: No Trainer in Session {session_code}!"
        elif urgency == "urgent":
            subject = f"🚨 URGENT: Session {session_code} Starting - No Trainer"
        else:
            subject = f"⚠️ Warning: Session {session_code} - Trainer Not Yet Joined"

        # Create email body
        body = self._format_email_body(alert)

        # Send to all recipients
        success = await self.send_email(
            to_emails=self.alert_recipients,
            subject=subject,
            body=body,
            is_html=True
        )

        if success:
            logger.info(f"[NOTIFY] Trainer alert sent for Session {session_code}")

        return success

    def _format_email_body(self, alert: Dict) -> str:
        """Format the email body for trainer alerts."""
        urgency = alert.get("urgency", "warning")
        session_code = alert.get("session_code", "Unknown")
        scheduled_start = alert.get("scheduled_start", "")
        student_count = alert.get("student_count", 0)
        has_students = alert.get("has_students", False)
        message = alert.get("message", "")

        # Parse scheduled time for display
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_start)
            time_str = scheduled_dt.strftime("%I:%M %p")
            date_str = scheduled_dt.strftime("%B %d, %Y")
        except:
            time_str = scheduled_start
            date_str = ""

        # Color based on urgency
        colors = {
            "warning": "#FFA500",  # Orange
            "urgent": "#FF6B00",   # Dark orange
            "critical": "#FF0000"  # Red
        }
        color = colors.get(urgency, "#FFA500")

        html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <div style="background-color: {color}; color: white; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <h2 style="margin: 0;">Trainer Alert - Session {session_code}</h2>
            </div>

            <div style="padding: 20px; background-color: #f5f5f5; border-radius: 8px;">
                <p style="font-size: 16px; margin-bottom: 15px;">
                    {message}
                </p>

                <table style="width: 100%; border-collapse: collapse;">
                    <tr>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Session:</strong></td>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;">Session {session_code}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Scheduled Time:</strong></td>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;">{time_str}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Date:</strong></td>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;">{date_str}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Students Waiting:</strong></td>
                        <td style="padding: 8px; border-bottom: 1px solid #ddd;">{student_count if has_students else 'None yet'}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px;"><strong>Alert Level:</strong></td>
                        <td style="padding: 8px; color: {color}; font-weight: bold;">{urgency.upper()}</td>
                    </tr>
                </table>
            </div>

            <div style="margin-top: 20px; padding: 15px; background-color: #fff3cd; border-radius: 8px; border-left: 4px solid #ffc107;">
                <strong>Action Required:</strong> Please ensure a trainer joins Session {session_code} immediately.
            </div>

            <p style="color: #666; font-size: 12px; margin-top: 20px;">
                This is an automated alert from the AALB Attendance System.
            </p>
        </body>
        </html>
        """

        return html

    async def send_email(
        self,
        to_emails: List[str],
        subject: str,
        body: str,
        is_html: bool = False
    ) -> bool:
        """
        Send an email to one or more recipients.

        Args:
            to_emails: List of recipient email addresses
            subject: Email subject
            body: Email body (plain text or HTML)
            is_html: Whether body is HTML

        Returns:
            True if sent successfully
        """
        if not self.smtp_user or not self.smtp_password:
            logger.error("[NOTIFY] SMTP credentials not configured. Set SMTP_USER and SMTP_PASSWORD.")
            return False

        try:
            # Create message
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.from_email
            msg["To"] = ", ".join(to_emails)

            # Attach body
            if is_html:
                msg.attach(MIMEText(body, "html"))
            else:
                msg.attach(MIMEText(body, "plain"))

            # Send via SMTP
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.from_email, to_emails, msg.as_string())

            logger.info(f"[NOTIFY] Email sent to {len(to_emails)} recipients: {subject}")
            return True

        except Exception as e:
            logger.error(f"[NOTIFY] Failed to send email: {e}")
            return False

    async def send_google_spaces_message(self, message: str) -> bool:
        """
        Send a message to Google Spaces via webhook.

        TODO: Implement when webhook URL is provided.
        """
        if not self.google_spaces_webhook:
            logger.warning("[NOTIFY] Google Spaces webhook not configured.")
            return False

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.google_spaces_webhook,
                    json={"text": message},
                    timeout=10
                )
                response.raise_for_status()

            logger.info("[NOTIFY] Google Spaces message sent")
            return True

        except Exception as e:
            logger.error(f"[NOTIFY] Failed to send Google Spaces message: {e}")
            return False


# Singleton instance
notification_service = NotificationService()
