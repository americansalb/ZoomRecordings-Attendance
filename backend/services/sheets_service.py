import os
from typing import Optional, List, Dict, Any
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class SheetsService:
    """Service for interacting with Google Sheets API"""

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    def __init__(self):
        self.shared_drive_id = os.getenv("GOOGLE_SHARED_DRIVE_ID")
        self._sheets_service = None
        self._drive_service = None

    def _get_credentials(self):
        """Get Google API credentials from environment variables or file"""
        # Try environment variables first (for Render/production)
        client_email = os.getenv("GOOGLE_CLIENT_EMAIL")
        private_key = os.getenv("GOOGLE_PRIVATE_KEY")

        if client_email and private_key:
            # Handle escaped newlines in private key
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

        # Fallback to credentials file (for local development)
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

    @property
    def drive(self):
        """Get the Drive API service"""
        if not self._drive_service:
            credentials = self._get_credentials()
            self._drive_service = build("drive", "v3", credentials=credentials)
        return self._drive_service

    def find_session_sheet(self, session_code: str) -> Optional[Dict[str, str]]:
        """
        Find a Google Sheet by session code (e.g., "127" -> "Session 127")

        Returns: {"id": "spreadsheet_id", "name": "Sheet Name"} or None
        """
        query = f"name contains 'Session {session_code}' and mimeType='application/vnd.google-apps.spreadsheet'"

        if self.shared_drive_id:
            query += f" and '{self.shared_drive_id}' in parents"

        try:
            results = self.drive.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives" if self.shared_drive_id else "user"
            ).execute()

            files = results.get("files", [])
            if files:
                return {"id": files[0]["id"], "name": files[0]["name"]}
            return None
        except HttpError as e:
            print(f"Error finding sheet: {e}")
            return None

    def create_session_sheet(self, session_code: str, title: Optional[str] = None) -> Dict[str, str]:
        """
        Create a new Google Sheet for a session

        Returns: {"id": "spreadsheet_id", "name": "Sheet Name"}
        """
        sheet_name = title or f"Session {session_code}"

        spreadsheet = {
            "properties": {"title": sheet_name},
            "sheets": [{
                "properties": {
                    "title": "Attendance",
                    "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 3}
                }
            }]
        }

        try:
            result = self.sheets.spreadsheets().create(body=spreadsheet).execute()
            spreadsheet_id = result["spreadsheetId"]

            # Set up header row
            headers = [["First Name", "Last Name", "Email"]]
            self.sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="Attendance!A1:C1",
                valueInputOption="RAW",
                body={"values": headers}
            ).execute()

            # Move to shared drive if configured
            if self.shared_drive_id:
                self.drive.files().update(
                    fileId=spreadsheet_id,
                    addParents=self.shared_drive_id,
                    supportsAllDrives=True
                ).execute()

            return {"id": spreadsheet_id, "name": sheet_name}
        except HttpError as e:
            print(f"Error creating sheet: {e}")
            raise

    def get_or_create_session_sheet(self, session_code: str, title: Optional[str] = None) -> Dict[str, str]:
        """Find existing sheet or create new one for session"""
        existing = self.find_session_sheet(session_code)
        if existing:
            return existing
        return self.create_session_sheet(session_code, title)

    def get_sheet_data(self, spreadsheet_id: str, range_name: str = "Attendance!A:ZZ") -> List[List[str]]:
        """Get all data from a sheet"""
        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name
            ).execute()
            return result.get("values", [])
        except HttpError as e:
            print(f"Error reading sheet: {e}")
            return []

    def get_profiles(self, spreadsheet_id: str) -> List[Dict[str, Any]]:
        """
        Get all student profiles from a session sheet

        Returns list of profiles with attendance data
        """
        data = self.get_sheet_data(spreadsheet_id)
        if not data or len(data) < 1:
            return []

        headers = data[0]
        profiles = []

        for row_idx, row in enumerate(data[1:], start=2):  # Start at row 2 (1-indexed for sheets)
            if not row or not any(row[:3]):  # Skip empty rows
                continue

            profile = {
                "row_number": row_idx,
                "first_name": row[0] if len(row) > 0 else "",
                "last_name": row[1] if len(row) > 1 else "",
                "email": row[2] if len(row) > 2 else "",
                "attendance": {}
            }

            # Parse attendance columns (format: "MM/DD Attendance", "MM/DD Participation")
            for col_idx, header in enumerate(headers[3:], start=3):
                if col_idx < len(row):
                    value = row[col_idx]
                    # Try to parse as number
                    try:
                        profile["attendance"][header] = int(float(value)) if value else 0
                    except ValueError:
                        profile["attendance"][header] = value

            profiles.append(profile)

        return profiles

    def find_profile_row(self, spreadsheet_id: str, first_name: str, last_name: str, email: str = "") -> Optional[int]:
        """
        Find a profile row by name (and optionally email)

        Returns row number (1-indexed) or None if not found
        """
        profiles = self.get_profiles(spreadsheet_id)

        for profile in profiles:
            # Match by email first (most reliable)
            if email and profile["email"].lower() == email.lower():
                return profile["row_number"]

            # Match by name
            if (profile["first_name"].lower() == first_name.lower() and
                    profile["last_name"].lower() == last_name.lower()):
                return profile["row_number"]

        return None

    def add_profile(self, spreadsheet_id: str, first_name: str, last_name: str, email: str = "") -> int:
        """
        Add a new profile to the sheet

        Returns: row number of the new profile
        """
        data = self.get_sheet_data(spreadsheet_id)
        new_row_number = len(data) + 1

        self.sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range="Attendance!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[first_name, last_name, email]]}
        ).execute()

        return new_row_number

    def get_or_add_date_columns(self, spreadsheet_id: str, date_str: str) -> Dict[str, int]:
        """
        Ensure attendance and participation columns exist for a date

        Args:
            date_str: Date string in MM/DD format

        Returns: {"attendance_col": col_index, "participation_col": col_index}
        """
        data = self.get_sheet_data(spreadsheet_id, "Attendance!1:1")
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

        # Add missing columns
        if attendance_col is None:
            attendance_col = len(headers)
            headers.append(attendance_header)

        if participation_col is None:
            participation_col = len(headers)
            headers.append(participation_header)

        # Update headers if we added new columns
        if attendance_col >= len(data[0]) if data else True or participation_col >= len(data[0]) if data else True:
            self.sheets.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range="Attendance!1:1",
                valueInputOption="RAW",
                body={"values": [headers]}
            ).execute()

        return {
            "attendance_col": attendance_col,
            "participation_col": participation_col
        }

    def update_attendance(self, spreadsheet_id: str, row_number: int,
                          attendance_col: int, minutes: int) -> None:
        """Update attendance minutes for a profile"""
        col_letter = self._col_index_to_letter(attendance_col)
        cell_range = f"Attendance!{col_letter}{row_number}"

        self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[minutes]]}
        ).execute()

    def update_participation(self, spreadsheet_id: str, row_number: int,
                             participation_col: int, minutes: int) -> None:
        """Update participation minutes for a profile"""
        col_letter = self._col_index_to_letter(participation_col)
        cell_range = f"Attendance!{col_letter}{row_number}"

        self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption="RAW",
            body={"values": [[minutes]]}
        ).execute()

    def batch_update_attendance(self, spreadsheet_id: str, updates: List[Dict]) -> None:
        """
        Batch update attendance for multiple profiles

        Args:
            updates: List of {"row": int, "col": int, "value": int}
        """
        data = []
        for update in updates:
            col_letter = self._col_index_to_letter(update["col"])
            data.append({
                "range": f"Attendance!{col_letter}{update['row']}",
                "values": [[update["value"]]]
            })

        if data:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data}
            ).execute()

    def merge_profiles(self, spreadsheet_id: str, keep_row: int, merge_row: int) -> None:
        """
        Merge two profiles, keeping one and deleting the other

        Combines attendance/participation data (takes max of each)
        """
        data = self.get_sheet_data(spreadsheet_id)
        if merge_row > len(data) or keep_row > len(data):
            raise ValueError("Invalid row numbers")

        keep_data = data[keep_row - 1]
        merge_data = data[merge_row - 1]
        headers = data[0]

        # Combine attendance data (take max for each date)
        combined = list(keep_data)
        while len(combined) < len(headers):
            combined.append("")

        for col_idx in range(3, len(headers)):
            keep_val = int(float(keep_data[col_idx])) if col_idx < len(keep_data) and keep_data[col_idx] else 0
            merge_val = int(float(merge_data[col_idx])) if col_idx < len(merge_data) and merge_data[col_idx] else 0
            combined[col_idx] = max(keep_val, merge_val)

        # Update the kept row
        self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"Attendance!A{keep_row}:{self._col_index_to_letter(len(combined) - 1)}{keep_row}",
            valueInputOption="RAW",
            body={"values": [combined]}
        ).execute()

        # Delete the merged row
        self.sheets.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [{
                    "deleteDimension": {
                        "range": {
                            "sheetId": 0,
                            "dimension": "ROWS",
                            "startIndex": merge_row - 1,
                            "endIndex": merge_row
                        }
                    }
                }]
            }
        ).execute()

    def update_profile(self, spreadsheet_id: str, row_number: int,
                       first_name: str, last_name: str, email: str) -> None:
        """Update profile information"""
        self.sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"Attendance!A{row_number}:C{row_number}",
            valueInputOption="RAW",
            body={"values": [[first_name, last_name, email]]}
        ).execute()

    def list_all_sheets(self) -> List[Dict[str, str]]:
        """List all session sheets in the shared drive"""
        query = "name contains 'Session' and mimeType='application/vnd.google-apps.spreadsheet'"

        try:
            results = self.drive.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives" if self.shared_drive_id else "user",
                orderBy="name"
            ).execute()

            return results.get("files", [])
        except HttpError as e:
            print(f"Error listing sheets: {e}")
            return []

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
