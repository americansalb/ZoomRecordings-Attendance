import httpx
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import base64
import re
from urllib.parse import quote

# Configure logging
logger = logging.getLogger(__name__)


class ZoomAccount:
    """Represents a single Zoom account configuration"""

    def __init__(self, account_id: str, name: str, zoom_account_id: str, client_id: str, client_secret: str):
        self.account_id = account_id
        self.name = name
        self.zoom_account_id = zoom_account_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None


class ZoomService:
    """Service for interacting with Zoom API with multi-account support"""

    BASE_URL = "https://api.zoom.us/v2"
    TOKEN_URL = "https://zoom.us/oauth/token"

    def __init__(self):
        self.accounts: Dict[str, ZoomAccount] = {}
        self._load_accounts_from_env()

        # Log configuration status on init
        logger.info(f"ZoomService initialized with {len(self.accounts)} account(s)")
        for account_id, account in self.accounts.items():
            logger.info(f"  - {account_id}: {account.name}")

    def _load_accounts_from_env(self):
        """Load all Zoom accounts from environment variables"""
        # Check for legacy single account (backwards compatibility)
        legacy_account_id = os.getenv("ZOOM_ACCOUNT_ID")
        legacy_client_id = os.getenv("ZOOM_CLIENT_ID")
        legacy_client_secret = os.getenv("ZOOM_CLIENT_SECRET")

        if legacy_account_id and legacy_client_id and legacy_client_secret:
            logger.info("Found legacy single account configuration")
            self.accounts["default"] = ZoomAccount(
                account_id="default",
                name="Default Account",
                zoom_account_id=legacy_account_id,
                client_id=legacy_client_id,
                client_secret=legacy_client_secret
            )

        # Load numbered accounts (ZOOM_ACCOUNT_1_ID, ZOOM_ACCOUNT_2_ID, etc.)
        account_number = 1
        while True:
            zoom_account_id = os.getenv(f"ZOOM_ACCOUNT_{account_number}_ID")
            client_id = os.getenv(f"ZOOM_ACCOUNT_{account_number}_CLIENT_ID")
            client_secret = os.getenv(f"ZOOM_ACCOUNT_{account_number}_CLIENT_SECRET")
            name = os.getenv(f"ZOOM_ACCOUNT_{account_number}_NAME", f"Account {account_number}")

            if not zoom_account_id or not client_id or not client_secret:
                break

            account_id = f"account-{account_number}"
            self.accounts[account_id] = ZoomAccount(
                account_id=account_id,
                name=name,
                zoom_account_id=zoom_account_id,
                client_id=client_id,
                client_secret=client_secret
            )

            logger.info(f"Loaded account {account_number}: {name}")
            account_number += 1

        if not self.accounts:
            logger.warning("No Zoom accounts configured!")

    def get_accounts(self) -> List[Dict[str, str]]:
        """Get list of all configured accounts"""
        return [
            {
                "id": account.account_id,
                "name": account.name
            }
            for account in self.accounts.values()
        ]

    async def list_users(self, account_id: Optional[str] = None) -> list:
        """
        Get list of all users in the Zoom account

        Args:
            account_id: Zoom account ID to use (optional, defaults to first account)

        Returns:
            List of users with id, email, first_name, last_name, type
        """
        account = self._get_account(account_id)
        print(f"[ZOOM] Fetching users for {account.name}...", flush=True)
        logger.info(f"Fetching users for {account.name}")

        try:
            users_response = await self._make_request("GET", "/users", account, params={"page_size": 300})
            users = users_response.get("users", [])
            print(f"[ZOOM] Found {len(users)} users in {account.name}", flush=True)
            logger.info(f"Found {len(users)} users in {account.name}")

            # Format user data
            formatted_users = []
            for user in users:
                formatted_users.append({
                    "id": user.get("id"),
                    "email": user.get("email", ""),
                    "first_name": user.get("first_name", ""),
                    "last_name": user.get("last_name", ""),
                    "display_name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get("email", ""),
                    "type": user.get("type", 1),  # 1=Basic, 2=Licensed, 3=On-prem
                    "status": user.get("status", ""),
                })

            return formatted_users
        except Exception as e:
            print(f"[ZOOM] Failed to fetch users for {account.name}: {str(e)}", flush=True)
            logger.error(f"Failed to fetch users list for {account.name}: {str(e)}")
            raise

    def _get_account(self, account_id: Optional[str] = None) -> ZoomAccount:
        """Get account by ID, or return first account if not specified"""
        if account_id:
            if account_id not in self.accounts:
                raise ValueError(f"Account '{account_id}' not found")
            return self.accounts[account_id]

        # Return first account if none specified
        if not self.accounts:
            raise ValueError("No Zoom accounts configured")

        return list(self.accounts.values())[0]

    async def _get_access_token(self, account: ZoomAccount) -> str:
        """Get or refresh the OAuth access token using Server-to-Server OAuth"""
        if account._access_token and account._token_expiry and datetime.now() < account._token_expiry:
            return account._access_token

        print(f"[ZOOM] Requesting new access token for {account.name}...", flush=True)
        logger.info(f"Requesting new Zoom access token for {account.name}...")

        # Create Basic Auth header
        credentials = f"{account.client_id}:{account.client_secret}"
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
                        "account_id": account.zoom_account_id
                    }
                )
                response.raise_for_status()
                data = response.json()

                account._access_token = data["access_token"]
                # Token expires in 1 hour, refresh 5 minutes early
                account._token_expiry = datetime.now() + timedelta(seconds=data.get("expires_in", 3600) - 300)

                print(f"[ZOOM] Successfully obtained access token for {account.name}", flush=True)
                logger.info(f"Successfully obtained Zoom access token for {account.name}")
                return account._access_token
            except httpx.HTTPStatusError as e:
                print(f"[ZOOM] Token request FAILED for {account.name}: {e.response.status_code} - {e.response.text}", flush=True)
                logger.error(f"Zoom token request failed for {account.name}: {e.response.status_code} - {e.response.text}")
                raise
            except Exception as e:
                print(f"[ZOOM] Token request ERROR for {account.name}: {str(e)}", flush=True)
                logger.error(f"Zoom token request error for {account.name}: {str(e)}")
                raise

    async def _make_request(self, method: str, endpoint: str, account: ZoomAccount, **kwargs) -> dict:
        """Make an authenticated request to Zoom API"""
        token = await self._get_access_token(account)

        url = f"{self.BASE_URL}{endpoint}"
        logger.info(f"Zoom API request ({account.name}): {method} {url}")

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
                logger.error(f"Zoom API error ({account.name}): {method} {endpoint} -> {e.response.status_code}: {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Zoom API request failed ({account.name}): {method} {endpoint} -> {str(e)}")
                raise

    async def list_recordings(self, user_id: str = "me", from_date: Optional[str] = None, to_date: Optional[str] = None, account_id: Optional[str] = None) -> dict:
        """
        List cloud recordings for a user

        Args:
            user_id: User ID or 'me' for the authenticated user
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)
            account_id: Zoom account ID to use (optional, defaults to first account)
        """
        account = self._get_account(account_id)
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

        return await self._make_request("GET", f"/users/{user_id}/recordings", account, params=params)

    async def list_all_recordings(self, from_date: Optional[str] = None, to_date: Optional[str] = None, account_id: Optional[str] = None) -> list:
        """
        List all cloud recordings across all users in the account

        Args:
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)
            account_id: Zoom account ID to use (optional, defaults to first account)
        """
        account = self._get_account(account_id)
        print(f"[ZOOM] list_all_recordings called for {account.name}: {from_date} to {to_date}", flush=True)
        logger.info(f"Fetching all recordings for {account.name} from {from_date} to {to_date}")

        # First get list of users
        try:
            print(f"[ZOOM] Fetching users list for {account.name}...", flush=True)
            users_response = await self._make_request("GET", "/users", account, params={"page_size": 300})
            users = users_response.get("users", [])
            print(f"[ZOOM] Found {len(users)} users in {account.name}", flush=True)
            logger.info(f"Found {len(users)} users in {account.name}")
        except Exception as e:
            print(f"[ZOOM] Failed to fetch users for {account.name}: {str(e)}", flush=True)
            logger.error(f"Failed to fetch users list for {account.name}: {str(e)}")
            raise

        all_recordings = []
        for user in users:
            try:
                recordings = await self.list_recordings(user["id"], from_date, to_date, account_id)
                meetings = recordings.get("meetings", [])
                logger.info(f"User {user.get('email', user['id'])} ({account.name}): {len(meetings)} recordings")
                for meeting in meetings:
                    meeting["host_email"] = user.get("email", "")
                    meeting["host_name"] = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                all_recordings.extend(meetings)
            except Exception as e:
                logger.warning(f"Error fetching recordings for user {user.get('email', user['id'])} in {account.name}: {e}")
                continue

        logger.info(f"Total recordings found for {account.name}: {len(all_recordings)}")
        return all_recordings

    async def get_meeting_participants(self, meeting_id: str, account_id: Optional[str] = None) -> dict:
        """
        Get participant report for a past meeting

        Args:
            meeting_id: Meeting UUID or ID
            account_id: Zoom account ID to use (optional, defaults to first account)

        Note: This endpoint requires the meeting to have ended and
        reports are available ~15 minutes after the meeting ends
        """
        account = self._get_account(account_id)

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
            params = {
                "page_size": 300,
                "include_fields": "registrant_id"  # Request all available fields
            }
            if next_page_token:
                params["next_page_token"] = next_page_token

            response = await self._make_request(
                "GET",
                f"/report/meetings/{clean_meeting_id}/participants",
                account,
                params=params
            )

            participants = response.get("participants", [])
            all_participants.extend(participants)

            # Debug: Log pagination info
            print(f"[ZOOM] Fetched {len(participants)} participants in this page, total so far: {len(all_participants)}", flush=True)
            print(f"[ZOOM] Response keys: {response.keys()}", flush=True)

            next_page_token = response.get("next_page_token")
            if not next_page_token:
                break

        print(f"[ZOOM] Final total participants: {len(all_participants)} records", flush=True)
        return {
            "participants": all_participants,
            "total_records": len(all_participants)
        }

    async def get_meeting_details(self, meeting_id: str, account_id: Optional[str] = None) -> dict:
        """Get details about a specific meeting"""
        account = self._get_account(account_id)
        clean_meeting_id = str(meeting_id)
        if "/" in clean_meeting_id or "=" in clean_meeting_id:
            clean_meeting_id = quote(quote(clean_meeting_id, safe=""), safe="")
        return await self._make_request("GET", f"/meetings/{clean_meeting_id}", account)

    async def get_past_meeting_details(self, meeting_id: str, account_id: Optional[str] = None) -> dict:
        """Get details about a past meeting instance - includes actual start/end times"""
        account = self._get_account(account_id)
        clean_meeting_id = str(meeting_id)
        if "/" in clean_meeting_id or "=" in clean_meeting_id:
            clean_meeting_id = quote(quote(clean_meeting_id, safe=""), safe="")
        result = await self._make_request("GET", f"/past_meetings/{clean_meeting_id}", account)
        print(f"[ZOOM] past_meetings response: {result}", flush=True)
        return result

    async def get_meeting_schedule(self, meeting_id: str, account_id: Optional[str] = None) -> dict:
        """Get scheduled meeting details - for recurring meetings, this has the scheduled times"""
        account = self._get_account(account_id)
        # Extract numeric meeting ID from UUID if needed (UUIDs are for instances, numeric IDs for the series)
        clean_meeting_id = str(meeting_id)
        if "/" in clean_meeting_id or "=" in clean_meeting_id:
            clean_meeting_id = quote(quote(clean_meeting_id, safe=""), safe="")

        result = await self._make_request("GET", f"/meetings/{clean_meeting_id}", account)
        print(f"[ZOOM] meetings (schedule) response: {result}", flush=True)
        return result

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
