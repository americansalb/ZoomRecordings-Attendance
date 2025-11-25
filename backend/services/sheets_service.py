import os
import logging
import time
from typing import Optional, List, Dict, Any
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


class SheetsService:
    """Service for interacting with Google Sheets API

    Uses ONE spreadsheet with multiple tabs (sheets) - one tab per session.
    """

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    def __init__(self):
        # The single spreadsheet ID that contains all session tabs (check both env var names)
        self.spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID") or os.getenv("GOOGLE_SHEET_ID")
        self._sheets_service = None
        self._sheet_id_cache = {}  # Cache tab name -> sheet ID mapping
        self._data_cache = {}  # Cache session_code -> {data, timestamp}
        self._cache_ttl = 30  # Cache TTL in seconds
        logger.info(f"SheetsService initialized - Spreadsheet ID: {'SET' if self.spreadsheet_id else 'MISSING'}")

    def _get_credentials(self):
        """Get Google API credentials from environment variables or file"""
        client_email = os.getenv("GOOGLE_CLIENT_EMAIL")
        private_key = os.getenv("GOOGLE_PRIVATE_KEY")

        if client_email and private_key:
            private_key = private_key.replace("\\n", "\n")
            credentials_info = {
                "type": "service_account",
                "client_email": client_email,
                "private_key": private_key,
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            return service_account.Credentials.from_service_account_info(
                credentials_info,
                scopes=self.SCOPES
            )

        credentials_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
        return service_account.Credentials.from_service_account_file(
            credentials_file,
            scopes=self.SCOPES
        )

    @property
    def sheets(self):
        """Get the Sheets API service"""
        if not self._sheets_service:
            credentials = self._get_credentials()
            self._sheets_service = build("sheets", "v4", credentials=credentials)
        return self._sheets_service

    def _get_all_tabs(self) -> List[Dict[str, Any]]:
        """Get all tabs in the spreadsheet"""
        try:
            result = self.sheets.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id
            ).execute()
            return result.get("sheets", [])
        except HttpError as e:
            print(f"Error getting tabs: {e}")
            return []

    def _get_sheet_id(self, tab_name: str) -> Optional[int]:
        """Get the numeric sheet ID for a tab name"""
        if tab_name in self._sheet_id_cache:
            return self._sheet_id_cache[tab_name]

        tabs = self._get_all_tabs()
        for tab in tabs:
            props = tab.get("properties", {})
            if props.get("title") == tab_name:
                sheet_id = props.get("sheetId")
                self._sheet_id_cache[tab_name] = sheet_id
                return sheet_id
        return None

    def find_session_tab(self, session_code: str) -> Optional[Dict[str, Any]]:
        """
        Find a tab by session code (e.g., "127" -> "Session 127")

        Returns: {"name": "Session 127", "sheet_id": 123} or None
        """
        tab_name = f"Session {session_code}"
        tabs = self._get_all_tabs()

        for tab in tabs:
            props = tab.get("properties", {})
            if props.get("title") == tab_name:
                return {
                    "name": tab_name,
                    "sheet_id": props.get("sheetId"),
                    "session_code": session_code
                }
        return None

    def create_session_tab(self, session_code: str) -> Dict[str, Any]:
        """
        Create a new tab for a session

        Returns: {"name": "Session 127", "sheet_id": 123}
        """
        tab_name = f"Session {session_code}"

        try:
            # Create the new tab
            request = {
                "requests": [{
                    "addSheet": {
                        "properties": {
                            "title": tab_name,
                            "gridProperties": {
                                "frozenRowCount": 1,
                                "frozenColumnCount": 3
                            }
                        }
                    }
                }]
            }

            result = self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=request
            ).execute()

            sheet_id = result["replies"][0]["addSheet"]["properties"]["sheetId"]

            # Set up header row
            headers = [["First Name", "Last Name", "Email"]]
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A1:C1",
                valueInputOption="RAW",
                body={"values": headers}
            ).execute()

            # Cache it
            self._sheet_id_cache[tab_name] = sheet_id

            return {
                "name": tab_name,
                "sheet_id": sheet_id,
                "session_code": session_code
            }

        except HttpError as e:
            print(f"Error creating tab: {e}")
            raise

    def get_or_create_session_tab(self, session_code: str) -> Dict[str, Any]:
        """Find existing tab or create new one for session"""
        existing = self.find_session_tab(session_code)
        if existing:
            return existing
        return self.create_session_tab(session_code)

    def get_tab_data(self, session_code: str, range_suffix: str = "A:ZZ", use_cache: bool = True) -> List[List[str]]:
        """Get all data from a session tab (with optional caching)"""
        tab_name = f"Session {session_code}"
        cache_key = f"{session_code}:{range_suffix}"

        # Check cache first
        if use_cache and cache_key in self._data_cache:
            cached = self._data_cache[cache_key]
            if time.time() - cached["timestamp"] < self._cache_ttl:
                return cached["data"]

        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!{range_suffix}"
            ).execute()
            data = result.get("values", [])

            # Cache the result
            self._data_cache[cache_key] = {
                "data": data,
                "timestamp": time.time()
            }
            return data
        except HttpError as e:
            print(f"Error reading tab: {e}")
            return []

    def invalidate_cache(self, session_code: str = None):
        """Invalidate cache for a session or all sessions"""
        if session_code:
            keys_to_remove = [k for k in self._data_cache.keys() if k.startswith(f"{session_code}:")]
            for key in keys_to_remove:
                del self._data_cache[key]
        else:
            self._data_cache.clear()

    def get_profiles(self, session_code: str) -> List[Dict[str, Any]]:
        """
        Get all student profiles from a session tab

        Returns list of profiles with attendance data
        """
        data = self.get_tab_data(session_code)
        if not data or len(data) < 1:
            return []

        headers = data[0]
        profiles = []

        for row_idx, row in enumerate(data[1:], start=2):
            if not row or not any(row[:3]):
                continue

            profile = {
                "row_number": row_idx,
                "first_name": row[0] if len(row) > 0 else "",
                "last_name": row[1] if len(row) > 1 else "",
                "email": row[2] if len(row) > 2 else "",
                "attendance": {}
            }

            for col_idx, header in enumerate(headers[3:], start=3):
                if col_idx < len(row):
                    value = row[col_idx]
                    try:
                        profile["attendance"][header] = int(float(value)) if value else 0
                    except ValueError:
                        profile["attendance"][header] = value

            profiles.append(profile)

        return profiles

    def find_profile_row(self, session_code: str, first_name: str, last_name: str, email: str = "") -> Optional[int]:
        """
        Find a profile row by name (and optionally email)

        Returns row number (1-indexed) or None if not found
        """
        profiles = self.get_profiles(session_code)

        for profile in profiles:
            if email and profile["email"].lower() == email.lower():
                return profile["row_number"]

            if (profile["first_name"].lower() == first_name.lower() and
                    profile["last_name"].lower() == last_name.lower()):
                return profile["row_number"]

        return None

    def add_profile(self, session_code: str, first_name: str, last_name: str, email: str = "") -> int:
        """
        Add a new profile to the session tab

        Returns: row number of the new profile
        """
        tab_name = f"Session {session_code}"
        data = self.get_tab_data(session_code)
        new_row_number = len(data) + 1

        self.sheets.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab_name}'!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[first_name, last_name, email]]}
        ).execute()

        return new_row_number

    def get_or_add_date_columns(self, session_code: str, date_str: str) -> Dict[str, int]:
        """
        Ensure attendance and participation columns exist for a date

        Returns: {"attendance_col": col_index, "participation_col": col_index}
        """
        tab_name = f"Session {session_code}"
        data = self.get_tab_data(session_code, "1:1")
        headers = data[0] if data else []

        attendance_header = f"{date_str} Attendance"
        participation_header = f"{date_str} Participation"

        attendance_col = None
        participation_col = None

        for idx, header in enumerate(headers):
            if header == attendance_header:
                attendance_col = idx
            elif header == participation_header:
                participation_col = idx

        if attendance_col is None:
            attendance_col = len(headers)
            headers.append(attendance_header)

        if participation_col is None:
            participation_col = len(headers)
            headers.append(participation_header)

        # Update headers if we added new columns
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab_name}'!1:1",
            valueInputOption="RAW",
            body={"values": [headers]}
        ).execute()

        return {
            "attendance_col": attendance_col,
            "participation_col": participation_col
        }

    def update_attendance(self, session_code: str, row_number: int,
                          attendance_col: int, minutes: int) -> None:
        """Update attendance minutes for a profile"""
        tab_name = f"Session {session_code}"
        col_letter = self._col_index_to_letter(attendance_col)
        cell_range = f"'{tab_name}'!{col_letter}{row_number}"

        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[minutes]]}
        ).execute()

    def update_participation(self, session_code: str, row_number: int,
                             participation_col: int, minutes: int) -> None:
        """Update participation minutes for a profile"""
        tab_name = f"Session {session_code}"
        col_letter = self._col_index_to_letter(participation_col)
        cell_range = f"'{tab_name}'!{col_letter}{row_number}"

        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[minutes]]}
        ).execute()

    def batch_update_attendance(self, session_code: str, updates: List[Dict]) -> None:
        """
        Batch update attendance for multiple profiles

        Args:
            updates: List of {"row": int, "col": int, "value": int}
        """
        tab_name = f"Session {session_code}"
        data = []
        for update in updates:
            col_letter = self._col_index_to_letter(update["col"])
            data.append({
                "range": f"'{tab_name}'!{col_letter}{update['row']}",
                "values": [[update["value"]]]
            })

        if data:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data}
            ).execute()

    def process_attendance_batch(self, session_code: str, date_str: str,
                                  participants: List[Dict]) -> Dict[str, Any]:
        """
        Process attendance for multiple participants in an efficient batch operation.

        This method minimizes API calls by:
        1. Reading sheet data ONCE
        2. Adding all new profiles in ONE batch append
        3. Adding date columns ONCE
        4. Updating all attendance values in ONE batch update

        Args:
            session_code: The session code (e.g., "126")
            date_str: The date string (e.g., "11/25")
            participants: List of {"first_name", "last_name", "email", "total_duration"}

        Returns:
            {"new_profiles": count, "updated_profiles": count, "profiles": [...]}
        """
        print(f"[SHEETS] Processing batch attendance for session {session_code}, {len(participants)} participants", flush=True)
        tab_name = f"Session {session_code}"

        # Step 1: Read existing data ONCE (bypass cache to get fresh data)
        data = self.get_tab_data(session_code, use_cache=False)
        headers = data[0] if data else ["First Name", "Last Name", "Email"]
        existing_rows = data[1:] if len(data) > 1 else []

        print(f"[SHEETS] Found {len(existing_rows)} existing profiles", flush=True)

        # Step 2: Build lookup index for existing profiles
        profile_index = {}  # (first_name_lower, last_name_lower) -> row_number
        email_index = {}    # email_lower -> row_number

        for row_idx, row in enumerate(existing_rows, start=2):
            if not row or not any(row[:3]):
                continue
            first_name = row[0].strip().lower() if len(row) > 0 and row[0] else ""
            last_name = row[1].strip().lower() if len(row) > 1 and row[1] else ""
            email = row[2].strip().lower() if len(row) > 2 and row[2] else ""

            if first_name and last_name:
                profile_index[(first_name, last_name)] = row_idx
            if email:
                email_index[email] = row_idx

        # Step 3: Find or add date columns
        attendance_header = f"{date_str} Attendance"
        participation_header = f"{date_str} Participation"

        attendance_col = None
        participation_col = None

        for idx, header in enumerate(headers):
            if header == attendance_header:
                attendance_col = idx
            elif header == participation_header:
                participation_col = idx

        headers_changed = False
        if attendance_col is None:
            attendance_col = len(headers)
            headers.append(attendance_header)
            headers_changed = True

        if participation_col is None:
            participation_col = len(headers)
            headers.append(participation_header)
            headers_changed = True

        # Update headers if needed (ONE API call)
        if headers_changed:
            print(f"[SHEETS] Adding date columns: {attendance_header}, {participation_header}", flush=True)
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!1:1",
                valueInputOption="RAW",
                body={"values": [headers]}
            ).execute()

        # Step 4: Categorize participants as new or existing
        new_profiles = []
        existing_updates = []
        results = {"new_profiles": 0, "updated_profiles": 0, "profiles": []}
        next_row = len(data) + 1  # Next available row

        for p in participants:
            first_name = p["first_name"].strip()
            last_name = p["last_name"].strip()
            email = p.get("email", "").strip()
            attendance_minutes = p["total_duration"] // 60

            # Try to find existing profile
            row_number = None

            # Check by email first
            if email and email.lower() in email_index:
                row_number = email_index[email.lower()]

            # Then by name
            if row_number is None:
                name_key = (first_name.lower(), last_name.lower())
                if name_key in profile_index:
                    row_number = profile_index[name_key]

            if row_number:
                # Existing profile - queue for update
                existing_updates.append({
                    "row": row_number,
                    "col": attendance_col,
                    "value": attendance_minutes
                })
                results["updated_profiles"] += 1
                results["profiles"].append({
                    "row": row_number,
                    "name": f"{first_name} {last_name}",
                    "email": email,
                    "attendance_minutes": attendance_minutes,
                    "is_new": False
                })
            else:
                # New profile - queue for batch add
                new_profiles.append([first_name, last_name, email])

                # Track the row number for this new profile
                row_number = next_row
                next_row += 1

                # Also queue attendance update for this new row
                existing_updates.append({
                    "row": row_number,
                    "col": attendance_col,
                    "value": attendance_minutes
                })

                results["new_profiles"] += 1
                results["profiles"].append({
                    "row": row_number,
                    "name": f"{first_name} {last_name}",
                    "email": email,
                    "attendance_minutes": attendance_minutes,
                    "is_new": True
                })

        # Step 5: Batch add all new profiles (ONE API call)
        if new_profiles:
            print(f"[SHEETS] Adding {len(new_profiles)} new profiles in batch", flush=True)
            self.sheets.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A:C",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": new_profiles}
            ).execute()

        # Step 6: Batch update all attendance values (ONE API call)
        if existing_updates:
            print(f"[SHEETS] Updating {len(existing_updates)} attendance values in batch", flush=True)
            batch_data = []
            for update in existing_updates:
                col_letter = self._col_index_to_letter(update["col"])
                batch_data.append({
                    "range": f"'{tab_name}'!{col_letter}{update['row']}",
                    "values": [[update["value"]]]
                })

            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": batch_data}
            ).execute()

        # Invalidate cache since we modified data
        self.invalidate_cache(session_code)

        print(f"[SHEETS] Batch complete: {results['new_profiles']} new, {results['updated_profiles']} updated", flush=True)
        return results

    def merge_profiles(self, session_code: str, keep_row: int, merge_row: int) -> None:
        """
        Merge two profiles, keeping one and deleting the other
        """
        tab_name = f"Session {session_code}"
        data = self.get_tab_data(session_code)
        if merge_row > len(data) or keep_row > len(data):
            raise ValueError("Invalid row numbers")

        keep_data = data[keep_row - 1]
        merge_data = data[merge_row - 1]
        headers = data[0]

        combined = list(keep_data)
        while len(combined) < len(headers):
            combined.append("")

        for col_idx in range(3, len(headers)):
            keep_val = int(float(keep_data[col_idx])) if col_idx < len(keep_data) and keep_data[col_idx] else 0
            merge_val = int(float(merge_data[col_idx])) if col_idx < len(merge_data) and merge_data[col_idx] else 0
            combined[col_idx] = max(keep_val, merge_val)

        # Update the kept row
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab_name}'!A{keep_row}:{self._col_index_to_letter(len(combined) - 1)}{keep_row}",
            valueInputOption="RAW",
            body={"values": [combined]}
        ).execute()

        # Delete the merged row
        sheet_id = self._get_sheet_id(tab_name)
        if sheet_id is not None:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "requests": [{
                        "deleteDimension": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "startIndex": merge_row - 1,
                                "endIndex": merge_row
                            }
                        }
                    }]
                }
            ).execute()

    def update_profile(self, session_code: str, row_number: int,
                       first_name: str, last_name: str, email: str) -> None:
        """Update profile information"""
        tab_name = f"Session {session_code}"
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"'{tab_name}'!A{row_number}:C{row_number}",
            valueInputOption="RAW",
            body={"values": [[first_name, last_name, email]]}
        ).execute()

    def list_all_sessions(self) -> List[Dict[str, str]]:
        """List all session tabs in the spreadsheet"""
        tabs = self._get_all_tabs()
        sessions = []

        for tab in tabs:
            props = tab.get("properties", {})
            title = props.get("title", "")

            # Check if it's a session tab (starts with "Session ")
            if title.startswith("Session "):
                session_code = title.replace("Session ", "")
                sessions.append({
                    "name": title,
                    "session_code": session_code,
                    "sheet_id": props.get("sheetId")
                })

        return sessions

    def get_spreadsheet_url(self) -> str:
        """Get the URL to the spreadsheet"""
        return f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"

    @staticmethod
    def _col_index_to_letter(index: int) -> str:
        """Convert column index (0-based) to letter (A, B, ..., Z, AA, AB, ...)"""
        result = ""
        while index >= 0:
            result = chr(index % 26 + ord('A')) + result
            index = index // 26 - 1
        return result


# Singleton instance
sheets_service = SheetsService()
