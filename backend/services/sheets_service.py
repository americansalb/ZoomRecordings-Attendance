import os
import logging
import time
import re
from typing import Optional, List, Dict, Any, Tuple
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from thefuzz import fuzz

logger = logging.getLogger(__name__)


class SheetsService:
    """Service for interacting with Google Sheets API

    Uses ONE spreadsheet with multiple tabs (sheets) - one tab per session.
    """

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    MAPPINGS_TAB_NAME = "Name Mappings"

    def __init__(self):
        # The single spreadsheet ID that contains all session tabs (check both env var names)
        self.spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID") or os.getenv("GOOGLE_SHEET_ID")
        # Master roster spreadsheet for canonical student names
        self.roster_spreadsheet_id = os.getenv("ROSTER_SPREADSHEET_ID")
        self._sheets_service = None
        self._sheet_id_cache = {}  # Cache tab name -> sheet ID mapping
        self._data_cache = {}  # Cache session_code -> {data, timestamp}
        self._roster_cache = {}  # Cache session_code -> roster data
        self._mappings_cache = {}  # Cache for name mappings
        self._cache_ttl = 30  # Cache TTL in seconds
        logger.info(f"SheetsService initialized - Spreadsheet ID: {'SET' if self.spreadsheet_id else 'MISSING'}")
        logger.info(f"Roster Spreadsheet ID: {'SET' if self.roster_spreadsheet_id else 'NOT SET'}")

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
            # Also clear roster cache for this session
            if session_code in self._roster_cache:
                del self._roster_cache[session_code]
        else:
            self._data_cache.clear()
            self._roster_cache.clear()

    # ==================== ROSTER METHODS ====================

    def get_roster(self, session_code: str) -> List[Dict[str, str]]:
        """
        Get the master roster for a session from the roster spreadsheet.

        Roster spreadsheet has tabs named "Session XXX" with columns:
        A: Student ID #, B: First Name, C: Last Name

        Returns list of {"student_id", "first_name", "last_name"}
        """
        if not self.roster_spreadsheet_id:
            print("[ROSTER] No roster spreadsheet configured", flush=True)
            return []

        # Check cache
        if session_code in self._roster_cache:
            cached = self._roster_cache[session_code]
            if time.time() - cached["timestamp"] < self._cache_ttl * 10:  # Longer TTL for roster
                return cached["data"]

        tab_name = f"Session {session_code}"
        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.roster_spreadsheet_id,
                range=f"'{tab_name}'!A:C"
            ).execute()
            rows = result.get("values", [])

            # Skip header row, parse roster
            roster = []
            for row in rows[1:]:  # Skip header
                if not row or len(row) < 2:
                    continue
                roster.append({
                    "student_id": row[0].strip() if len(row) > 0 and row[0] else "",
                    "first_name": row[1].strip() if len(row) > 1 and row[1] else "",
                    "last_name": row[2].strip() if len(row) > 2 and row[2] else ""
                })

            print(f"[ROSTER] Loaded {len(roster)} students for session {session_code}", flush=True)

            # Cache it
            self._roster_cache[session_code] = {
                "data": roster,
                "timestamp": time.time()
            }
            return roster

        except HttpError as e:
            print(f"[ROSTER] Error loading roster for session {session_code}: {e}", flush=True)
            return []

    def _normalize_name(self, name: str) -> str:
        """
        Normalize a name for matching:
        - Lowercase
        - Remove parenthetical content like "(Spanish)", "(Arabic)"
        - Remove common suffixes like 's iPhone, 's iPad
        - Strip extra whitespace
        """
        if not name:
            return ""

        # Lowercase
        name = name.lower()

        # Remove parenthetical content
        name = re.sub(r'\([^)]*\)', '', name)

        # Remove device suffixes
        name = re.sub(r"'s\s*(iphone|ipad|macbook|laptop|pc|webcam)", '', name, flags=re.IGNORECASE)

        # Remove underscores (like "Dilorom_Russian")
        name = name.replace('_', ' ')

        # Remove language indicators without parentheses
        languages = ['spanish', 'arabic', 'french', 'russian', 'chinese', 'haitian', 'creole', 'asl']
        for lang in languages:
            name = re.sub(rf'\b{lang}\b', '', name, flags=re.IGNORECASE)

        # Clean up whitespace
        name = ' '.join(name.split())

        return name.strip()

    def match_to_roster(self, first_name: str, last_name: str, roster: List[Dict],
                        threshold: int = 80) -> Optional[Dict]:
        """
        Match a Zoom participant name to a roster entry using fuzzy matching.

        Args:
            first_name: First name from Zoom
            last_name: Last name from Zoom (might be empty or abbreviated)
            roster: List of roster entries
            threshold: Minimum fuzzy match score (0-100)

        Returns:
            Matched roster entry or None
        """
        if not roster:
            return None

        # Normalize the input names
        norm_first = self._normalize_name(first_name)
        norm_last = self._normalize_name(last_name)

        best_match = None
        best_score = 0

        for entry in roster:
            roster_first = entry["first_name"].lower()
            roster_last = entry["last_name"].lower()

            # Score first name match (most important)
            first_score = fuzz.ratio(norm_first, roster_first)

            # If first name is a strong match, check last name
            if first_score >= threshold:
                # Last name matching strategies:
                # 1. Exact match
                # 2. Fuzzy match
                # 3. Initial match (e.g., "R" matches "Reisman")
                # 4. Last name is empty (just first name in Zoom)

                last_score = 0

                if not norm_last:
                    # No last name provided - rely on first name only
                    last_score = 50  # Partial credit
                elif len(norm_last) == 1:
                    # Single character - treat as initial
                    if roster_last and roster_last[0] == norm_last:
                        last_score = 90
                else:
                    # Full last name - fuzzy match
                    last_score = fuzz.ratio(norm_last, roster_last)

                # Combined score (weighted toward first name)
                combined_score = (first_score * 0.6) + (last_score * 0.4)

                if combined_score > best_score:
                    best_score = combined_score
                    best_match = entry

        # Only return if we have a good enough match
        if best_score >= threshold:
            print(f"[ROSTER] ✓ Matched '{first_name} {last_name}' -> '{best_match['first_name']} {best_match['last_name']}' (score: {best_score:.0f})", flush=True)
            return best_match

        print(f"[ROSTER] ✗ No match for '{first_name} {last_name}' (best score: {best_score:.0f})", flush=True)
        return None

    # ==================== NAME MAPPINGS METHODS ====================

    def _ensure_mappings_tab(self) -> None:
        """Ensure the Name Mappings tab exists, create if not"""
        tabs = self._get_all_tabs()
        for tab in tabs:
            if tab.get("properties", {}).get("title") == self.MAPPINGS_TAB_NAME:
                return  # Tab exists

        # Create the mappings tab
        try:
            request = {
                "requests": [{
                    "addSheet": {
                        "properties": {
                            "title": self.MAPPINGS_TAB_NAME,
                            "gridProperties": {
                                "frozenRowCount": 1
                            }
                        }
                    }
                }]
            }
            result = self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=request
            ).execute()

            # Set up header row
            headers = [["Zoom Name", "Student ID", "First Name", "Last Name", "Session Code", "Created At"]]
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.MAPPINGS_TAB_NAME}'!A1:F1",
                valueInputOption="RAW",
                body={"values": headers}
            ).execute()

            print(f"[MAPPINGS] Created '{self.MAPPINGS_TAB_NAME}' tab", flush=True)
        except HttpError as e:
            print(f"[MAPPINGS] Error creating mappings tab: {e}", flush=True)
            raise

    def get_name_mappings(self, session_code: str = None) -> List[Dict[str, str]]:
        """
        Get all name mappings, optionally filtered by session code.

        Returns list of {"zoom_name", "student_id", "first_name", "last_name", "session_code", "created_at"}
        """
        cache_key = f"mappings:{session_code or 'all'}"
        if cache_key in self._mappings_cache:
            cached = self._mappings_cache[cache_key]
            if time.time() - cached["timestamp"] < self._cache_ttl:
                return cached["data"]

        self._ensure_mappings_tab()

        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.MAPPINGS_TAB_NAME}'!A:F"
            ).execute()
            rows = result.get("values", [])

            mappings = []
            for row_idx, row in enumerate(rows[1:], start=2):  # Skip header
                if not row or not row[0]:
                    continue

                mapping = {
                    "row_number": row_idx,
                    "zoom_name": row[0].strip() if len(row) > 0 else "",
                    "student_id": row[1].strip() if len(row) > 1 else "",
                    "first_name": row[2].strip() if len(row) > 2 else "",
                    "last_name": row[3].strip() if len(row) > 3 else "",
                    "session_code": row[4].strip() if len(row) > 4 else "",
                    "created_at": row[5].strip() if len(row) > 5 else ""
                }

                # Filter by session code if specified
                if session_code is None or not mapping["session_code"] or mapping["session_code"] == session_code:
                    mappings.append(mapping)

            # Cache the result
            self._mappings_cache[cache_key] = {
                "data": mappings,
                "timestamp": time.time()
            }

            print(f"[MAPPINGS] Loaded {len(mappings)} name mappings", flush=True)
            return mappings

        except HttpError as e:
            print(f"[MAPPINGS] Error loading mappings: {e}", flush=True)
            return []

    def add_name_mapping(self, zoom_name: str, student_id: str, first_name: str,
                          last_name: str, session_code: str = "") -> Dict[str, Any]:
        """
        Add a new name mapping.

        Args:
            zoom_name: The Zoom display name to map (e.g., "Jamie R (Spanish)")
            student_id: The roster student ID
            first_name: Canonical first name
            last_name: Canonical last name
            session_code: Optional session code to limit scope

        Returns:
            The created mapping
        """
        self._ensure_mappings_tab()

        from datetime import datetime
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        new_row = [zoom_name, student_id, first_name, last_name, session_code, created_at]

        try:
            self.sheets.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.MAPPINGS_TAB_NAME}'!A:F",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [new_row]}
            ).execute()

            # Invalidate cache
            self._mappings_cache.clear()

            print(f"[MAPPINGS] Added mapping: '{zoom_name}' -> '{first_name} {last_name}'", flush=True)

            return {
                "zoom_name": zoom_name,
                "student_id": student_id,
                "first_name": first_name,
                "last_name": last_name,
                "session_code": session_code,
                "created_at": created_at
            }

        except HttpError as e:
            print(f"[MAPPINGS] Error adding mapping: {e}", flush=True)
            raise

    def delete_name_mapping(self, zoom_name: str) -> bool:
        """
        Delete a name mapping by Zoom name.

        Returns True if deleted, False if not found.
        """
        self._ensure_mappings_tab()

        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{self.MAPPINGS_TAB_NAME}'!A:A"
            ).execute()
            rows = result.get("values", [])

            # Find the row with matching zoom_name
            row_to_delete = None
            for row_idx, row in enumerate(rows[1:], start=2):  # Skip header
                if row and row[0].strip().lower() == zoom_name.strip().lower():
                    row_to_delete = row_idx
                    break

            if row_to_delete is None:
                return False

            # Delete the row
            sheet_id = self._get_sheet_id(self.MAPPINGS_TAB_NAME)
            if sheet_id is not None:
                self.sheets.spreadsheets().batchUpdate(
                    spreadsheetId=self.spreadsheet_id,
                    body={
                        "requests": [{
                            "deleteDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": row_to_delete - 1,
                                    "endIndex": row_to_delete
                                }
                            }
                        }]
                    }
                ).execute()

                # Invalidate cache
                self._mappings_cache.clear()

                print(f"[MAPPINGS] Deleted mapping for '{zoom_name}'", flush=True)
                return True

            return False

        except HttpError as e:
            print(f"[MAPPINGS] Error deleting mapping: {e}", flush=True)
            raise

    def find_mapping_for_name(self, zoom_name: str, session_code: str = None) -> Optional[Dict]:
        """
        Find a mapping for a given Zoom name.

        Args:
            zoom_name: The full Zoom display name
            session_code: Optional session to filter mappings

        Returns:
            Mapping entry or None
        """
        mappings = self.get_name_mappings(session_code)

        # Normalize for comparison
        norm_zoom = zoom_name.strip().lower()

        for mapping in mappings:
            if mapping["zoom_name"].lower() == norm_zoom:
                return mapping

        return None

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
        # Also build an index for initial-based matching: (first_name_lower, last_initial) -> row_number
        initial_index = {}  # (first_name_lower, last_initial) -> row_number

        for row_idx, row in enumerate(existing_rows, start=2):
            if not row or not any(row[:3]):
                continue
            first_name = row[0].strip().lower() if len(row) > 0 and row[0] else ""
            last_name = row[1].strip().lower() if len(row) > 1 and row[1] else ""
            email = row[2].strip().lower() if len(row) > 2 and row[2] else ""

            if first_name and last_name:
                profile_index[(first_name, last_name)] = row_idx
                # Also index by first name + last initial for initial-based matching
                if len(last_name) >= 1:
                    initial_index[(first_name, last_name[0])] = row_idx
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

        # Step 4: Load name mappings and roster for canonical name matching
        mappings = self.get_name_mappings(session_code)
        roster = self.get_roster(session_code)
        roster_matched_names = {}  # Track which roster entries we've already matched to existing profiles

        # Step 5: Categorize participants as new or existing
        new_profiles = []
        existing_updates = []
        results = {"new_profiles": 0, "updated_profiles": 0, "roster_matched": 0, "mapping_matched": 0, "unmatched": 0, "profiles": []}
        next_row = len(data) + 1  # Next available row

        for p in participants:
            zoom_first = p["first_name"].strip()
            zoom_last = p["last_name"].strip()
            email = p.get("email", "").strip()
            zoom_full_name = f"{zoom_first} {zoom_last}".strip()
            attendance_minutes = p["total_duration"] // 60

            # Use canonical name from roster if we can match
            first_name = zoom_first
            last_name = zoom_last
            roster_match = None
            mapping_match = None

            # First check for explicit name mappings (highest priority)
            mapping_match = self.find_mapping_for_name(zoom_full_name, session_code)
            if mapping_match:
                first_name = mapping_match["first_name"]
                last_name = mapping_match["last_name"]
                results["mapping_matched"] += 1
                print(f"[MAPPINGS] ✓ Used mapping: '{zoom_full_name}' -> '{first_name} {last_name}'", flush=True)
            elif roster:
                # Fall back to fuzzy roster matching
                roster_match = self.match_to_roster(zoom_first, zoom_last, roster)
                if roster_match:
                    first_name = roster_match["first_name"]
                    last_name = roster_match["last_name"]
                    results["roster_matched"] += 1

            # Try to find existing profile
            row_number = None

            # Check by email first
            if email and email.lower() in email_index:
                row_number = email_index[email.lower()]

            # Then by canonical name (from roster or original)
            if row_number is None:
                name_key = (first_name.lower(), last_name.lower())
                if name_key in profile_index:
                    row_number = profile_index[name_key]

            # If roster has an initial (single char last name), try initial-based matching
            # This handles cases like roster "Jamie R" matching existing "Jamie Reisman"
            if row_number is None and roster_match and len(last_name) == 1:
                initial_key = (first_name.lower(), last_name.lower())
                if initial_key in initial_index:
                    row_number = initial_index[initial_key]
                    print(f"[SHEETS] ✓ Initial match: '{first_name} {last_name}' found existing profile at row {row_number}", flush=True)

            # Also try original Zoom name if different from canonical
            if row_number is None and roster_match:
                orig_key = (zoom_first.lower(), zoom_last.lower())
                if orig_key in profile_index:
                    row_number = profile_index[orig_key]

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
                    "zoom_name": zoom_full_name if (roster_match or mapping_match) else None,
                    "email": email,
                    "attendance_minutes": attendance_minutes,
                    "is_new": False,
                    "roster_matched": roster_match is not None,
                    "mapping_matched": mapping_match is not None
                })
            else:
                # New profile - use canonical name from roster
                new_profiles.append([first_name, last_name, email])

                # Track for duplicate detection
                name_key = (first_name.lower(), last_name.lower())
                profile_index[name_key] = next_row

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
                if not roster_match and not mapping_match:
                    results["unmatched"] += 1
                results["profiles"].append({
                    "row": row_number,
                    "name": f"{first_name} {last_name}",
                    "zoom_name": zoom_full_name if (roster_match or mapping_match) else None,
                    "email": email,
                    "attendance_minutes": attendance_minutes,
                    "is_new": True,
                    "roster_matched": roster_match is not None,
                    "mapping_matched": mapping_match is not None
                })

        # Step 6: Batch add all new profiles (ONE API call)
        if new_profiles:
            print(f"[SHEETS] Adding {len(new_profiles)} new profiles in batch", flush=True)
            self.sheets.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A:C",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": new_profiles}
            ).execute()

        # Step 7: Batch update all attendance values (ONE API call)
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

        print(f"[SHEETS] Batch complete: {results['new_profiles']} new, {results['updated_profiles']} updated, {results['roster_matched']} roster-matched, {results['mapping_matched']} mapping-matched, {results['unmatched']} unmatched", flush=True)
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
