import os
from typing import Optional, List, Dict, Any
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class SheetsService:
    """Service for interacting with Google Sheets API

    Uses ONE spreadsheet with multiple tabs (sheets) - one tab per session.
    """

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    def __init__(self):
        # The single spreadsheet ID that contains all session tabs
        self.spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
        self._sheets_service = None
        self._sheet_id_cache = {}  # Cache tab name -> sheet ID mapping

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

    def get_tab_data(self, session_code: str, range_suffix: str = "A:ZZ") -> List[List[str]]:
        """Get all data from a session tab"""
        tab_name = f"Session {session_code}"
        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!{range_suffix}"
            ).execute()
            return result.get("values", [])
        except HttpError as e:
            print(f"Error reading tab: {e}")
            return []

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
