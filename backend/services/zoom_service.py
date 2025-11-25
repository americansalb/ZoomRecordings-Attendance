import httpx
import os
import logging
from datetime import datetime, timedelta
from typing import Optional
import base64
import re
from urllib.parse import quote

# Configure logging
logger = logging.getLogger(__name__)


class ZoomService:
    """Service for interacting with Zoom API"""

    BASE_URL = "https://api.zoom.us/v2"
    TOKEN_URL = "https://zoom.us/oauth/token"

    def __init__(self):
        self.account_id = os.getenv("ZOOM_ACCOUNT_ID")
        self.client_id = os.getenv("ZOOM_CLIENT_ID")
        self.client_secret = os.getenv("ZOOM_CLIENT_SECRET")
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

        # Log configuration status on init
        logger.info(f"ZoomService initialized - Account ID: {'SET' if self.account_id else 'MISSING'}, "
                   f"Client ID: {'SET' if self.client_id else 'MISSING'}, "
                   f"Client Secret: {'SET' if self.client_secret else 'MISSING'}")

    async def _get_access_token(self) -> str:
        """Get or refresh the OAuth access token using Server-to-Server OAuth"""
        if self._access_token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._access_token

        print("[ZOOM] Requesting new access token...", flush=True)
        logger.info("Requesting new Zoom access token...")

        if not self.client_id or not self.client_secret or not self.account_id:
            print(f"[ZOOM] ERROR: Missing credentials! account_id={bool(self.account_id)}, client_id={bool(self.client_id)}, client_secret={bool(self.client_secret)}", flush=True)
            logger.error("Missing Zoom credentials - cannot authenticate")
            raise ValueError("Missing Zoom credentials: ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, or ZOOM_CLIENT_SECRET not set")

        # Create Basic Auth header
        credentials = f"{self.client_id}:{self.client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.TOKEN_URL,
                    headers={
                        "Authorization": f"Basic {encoded_credentials}",
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    data={
                        "grant_type": "account_credentials",
                        "account_id": self.account_id
                    }
                )
                response.raise_for_status()
                data = response.json()

                self._access_token = data["access_token"]
                # Token expires in 1 hour, refresh 5 minutes early
                self._token_expiry = datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 300)

                print("[ZOOM] Successfully obtained access token", flush=True)
                logger.info("Successfully obtained Zoom access token")
                return self._access_token
            except httpx.HTTPStatusError as e:
                print(f"[ZOOM] Token request FAILED: {e.response.status_code} - {e.response.text}", flush=True)
                logger.error(f"Zoom token request failed: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                print(f"[ZOOM] Token request ERROR: {str(e)}", flush=True)
                logger.error(f"Zoom token request error: {str(e)}")
                raise

    async def _make_request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make an authenticated request to Zoom API"""
        token = await self._get_access_token()

        url = f"{self.BASE_URL}{endpoint}"
        logger.info(f"Zoom API request: {method} {url}")

        async with httpx.AsyncClient() as client:
            try:
                response = await client.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"
                    },
                    **kwargs
                )
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"Zoom API error: {method} {endpoint} -> {e.response.status_code}: {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Zoom API request failed: {method} {endpoint} -> {str(e)}")
                raise

    async def list_recordings(self, user_id: str = "me", from_date: Optional[str] = None, to_date: Optional[str] = None) -> dict:
        """
        List cloud recordings for a user

        Args:
            user_id: User ID or 'me' for the authenticated user
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)
        """
        params = {"page_size": 100}

        if from_date:
            params["from"] = from_date
        else:
            # Default to last 30 days
            params["from"] = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        if to_date:
            params["to"] = to_date
        else:
            params["to"] = datetime.now().strftime("%Y-%m-%d")

        return await self._make_request("GET", f"/users/{user_id}/recordings", params=params)

    async def list_all_recordings(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> list:
        """
        List all cloud recordings across all users in the account
        """
        print(f"[ZOOM] list_all_recordings called: {from_date} to {to_date}", flush=True)
        logger.info(f"Fetching all recordings from {from_date} to {to_date}")

        # First get list of users
        try:
            print("[ZOOM] Fetching users list...", flush=True)
            users_response = await self._make_request("GET", "/users", params={"page_size": 300})
            users = users_response.get("users", [])
            print(f"[ZOOM] Found {len(users)} users", flush=True)
            logger.info(f"Found {len(users)} users in Zoom account")
        except Exception as e:
            print(f"[ZOOM] Failed to fetch users: {str(e)}", flush=True)
            logger.error(f"Failed to fetch users list: {str(e)}")
            raise

        all_recordings = []
        for user in users:
            try:
                recordings = await self.list_recordings(user["id"], from_date, to_date)
                meetings = recordings.get("meetings", [])
                logger.info(f"User {user.get('email', user['id'])}: {len(meetings)} recordings")
                for meeting in meetings:
                    meeting["host_email"] = user.get("email", "")
                    meeting["host_name"] = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                all_recordings.extend(meetings)
            except Exception as e:
                logger.warning(f"Error fetching recordings for user {user.get('email', user['id'])}: {e}")
                continue

        logger.info(f"Total recordings found: {len(all_recordings)}")
        return all_recordings

    async def get_meeting_participants(self, meeting_id: str) -> dict:
        """
        Get participant report for a past meeting

        Note: This endpoint requires the meeting to have ended and
        reports are available ~15 minutes after the meeting ends
        """
        # For UUIDs with "/" or "==" characters, Zoom requires double URL encoding
        # e.g., "abc/def==" -> "abc%2Fdef%3D%3D" -> "abc%252Fdef%253D%253D"
        clean_meeting_id = str(meeting_id)
        if "/" in clean_meeting_id or "=" in clean_meeting_id:
            # Double URL encode for Zoom API
            clean_meeting_id = quote(quote(clean_meeting_id, safe=""), safe="")
            print(f"[ZOOM] UUID double-encoded: {meeting_id} -> {clean_meeting_id}", flush=True)

        all_participants = []
        next_page_token = None

        while True:
            params = {"page_size": 300}
            if next_page_token:
                params["next_page_token"] = next_page_token

            response = await self._make_request(
                "GET",
                f"/report/meetings/{clean_meeting_id}/participants",
                params=params
            )

            participants = response.get("participants", [])
            all_participants.extend(participants)

            next_page_token = response.get("next_page_token")
            if not next_page_token:
                break

        return {
            "participants": all_participants,
            "total_records": len(all_participants)
        }

    async def get_meeting_details(self, meeting_id: str) -> dict:
        """Get details about a specific meeting"""
        clean_meeting_id = str(meeting_id)
        if "/" in clean_meeting_id or "=" in clean_meeting_id:
            clean_meeting_id = quote(quote(clean_meeting_id, safe=""), safe="")
        return await self._make_request("GET", f"/meetings/{clean_meeting_id}")

    async def get_past_meeting_details(self, meeting_id: str) -> dict:
        """Get details about a past meeting instance"""
        clean_meeting_id = str(meeting_id)
        if "/" in clean_meeting_id or "=" in clean_meeting_id:
            clean_meeting_id = quote(quote(clean_meeting_id, safe=""), safe="")
        return await self._make_request("GET", f"/past_meetings/{clean_meeting_id}")

    @staticmethod
    def extract_session_code(title: str) -> Optional[str]:
        """
        Extract the 3-digit session code from a recording title

        Example: "Session 127. Mondays, Wednesdays..." -> "127"
        """
        match = re.search(r"Session\s*(\d{3})", title, re.IGNORECASE)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def calculate_attendance_minutes(join_time: str, leave_time: str,
                                      scheduled_start: Optional[datetime] = None,
                                      scheduled_end: Optional[datetime] = None) -> int:
        """
        Calculate the number of minutes a participant attended
        Only counts time within the scheduled meeting window
        """
        join_dt = datetime.fromisoformat(join_time.replace("Z", "+00:00"))
        leave_dt = datetime.fromisoformat(leave_time.replace("Z", "+00:00"))

        # If scheduled times provided, clamp to those bounds
        if scheduled_start:
            join_dt = max(join_dt, scheduled_start)
        if scheduled_end:
            leave_dt = min(leave_dt, scheduled_end)

        # Calculate duration in minutes
        duration = (leave_dt - join_dt).total_seconds() / 60
        return max(0, int(duration))


# Singleton instance
zoom_service = ZoomService()
