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
                                "frozenColumnCount": 4
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

            # Set up header row (Student ID in column A for ID-based matching)
            headers = [["Student ID", "First Name", "Last Name", "Email", "Roster Match"]]
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A1:E1",
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

    def _extract_student_id(self, name: str) -> str:
        """
        Extract a 5-digit student ID from a Zoom display name.

        Students are required to include their 5-digit ID in their Zoom name,
        e.g., "12345 John Smith", "John Smith 12345", "12345-John Smith".

        Returns the 5-digit ID string or empty string if not found.
        """
        if not name:
            return ""
        # Match a standalone 5-digit number (not part of a longer number)
        match = re.search(r'(?<!\d)\d{5}(?!\d)', name)
        return match.group(0) if match else ""

    def _normalize_name(self, name: str) -> str:
        """
        Normalize a name for matching:
        - Lowercase
        - Remove parenthetical content like "(Spanish)", "(Arabic)"
        - Remove common suffixes like 's iPhone, 's iPad, or just 's
        - Remove dash separators like "- BR Portuguese"
        - Strip extra whitespace
        """
        if not name:
            return ""

        # Lowercase
        name = name.lower()

        # Remove parenthetical content
        name = re.sub(r'\([^)]*\)', '', name)

        # Remove dash-separated language/location indicators (e.g., "- BR Portuguese")
        name = re.sub(r'\s*-\s*[a-z]+(\s+[a-z]+)*\s*$', '', name, flags=re.IGNORECASE)

        # Remove device suffixes (e.g., "'s iPhone", "'s iPad")
        name = re.sub(r"'s\s*(iphone|ipad|macbook|laptop|pc|webcam)", '', name, flags=re.IGNORECASE)

        # Remove standalone possessive 's (e.g., "Chrisnove's" -> "Chrisnove")
        name = re.sub(r"'s\s*$", '', name)
        name = re.sub(r"'s\s+", ' ', name)

        # Remove underscores (like "Dilorom_Russian" -> "Dilorom Russian")
        name = name.replace('_', ' ')

        # Remove language indicators without parentheses
        languages = ['spanish', 'arabic', 'french', 'russian', 'chinese', 'haitian', 'creole', 'asl', 'portuguese', 'vietnamese', 'hakha', 'chin', 'br']
        for lang in languages:
            name = re.sub(rf'\b{lang}\b', '', name, flags=re.IGNORECASE)

        # Clean up whitespace
        name = ' '.join(name.split())

        return name.strip()

    # Common nickname mappings (normalized lowercase)
    NICKNAME_MAP = {
        "gaby": "gabriela",
        "gabby": "gabriela",
        "lisbety": "lisbet",
        "lisbeth": "lisbet",
        "beth": "lisbet",
        "chris": "christine",
        "christy": "christine",
        "mike": "michael",
        "mikey": "michael",
        "nick": "nicholas",
        "nicky": "nicholas",
        "alex": "alexander",
        "alex": "alexandra",
        "will": "william",
        "bill": "william",
        "bob": "robert",
        "rob": "robert",
        "bobby": "robert",
        "joe": "joseph",
        "joey": "joseph",
        "tony": "anthony",
        "dan": "daniel",
        "danny": "daniel",
        "dave": "david",
        "davey": "david",
        "jim": "james",
        "jimmy": "james",
        "jen": "jennifer",
        "jenny": "jennifer",
        "kate": "katherine",
        "katie": "katherine",
        "kathy": "katherine",
        "liz": "elizabeth",
        "lizzy": "elizabeth",
        "beth": "elizabeth",
        "matt": "matthew",
        "matty": "matthew",
        "sam": "samuel",
        "sammy": "samuel",
        "tom": "thomas",
        "tommy": "thomas",
        "ed": "edward",
        "eddie": "edward",
        "ted": "theodore",
        "teddy": "theodore",
        "andy": "andrew",
        "drew": "andrew",
        "steve": "steven",
        "stevie": "steven",
        "pat": "patricia",
        "patty": "patricia",
        "trish": "patricia",
        "sue": "susan",
        "susie": "susan",
        "vicky": "victoria",
        "vic": "victoria",
    }

    def _get_nickname_variants(self, name: str) -> List[str]:
        """Get all nickname variants for a name (including the original)."""
        name_lower = name.lower().strip()
        variants = [name_lower]

        # Check if this name is a nickname, get the full name
        if name_lower in self.NICKNAME_MAP:
            variants.append(self.NICKNAME_MAP[name_lower])

        # Check if this name has nicknames (reverse lookup)
        for nick, full in self.NICKNAME_MAP.items():
            if full == name_lower:
                variants.append(nick)

        return variants

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
        # Combined name for two-word first name matching (e.g., "Van Daisy")
        combined_zoom = f"{norm_first} {norm_last}".strip()

        # Get nickname variants for matching
        first_variants = self._get_nickname_variants(norm_first)

        best_match = None
        best_score = 0

        for entry in roster:
            roster_first = entry["first_name"].lower().strip()
            roster_last = entry["last_name"].lower().strip()
            roster_full = f"{roster_first} {roster_last}".strip()

            # Get nickname variants for roster name too
            roster_first_variants = self._get_nickname_variants(roster_first)

            first_score = 0

            # Strategy 1: Direct first name match (including nickname variants)
            for zoom_variant in first_variants:
                for roster_variant in roster_first_variants:
                    score = fuzz.ratio(zoom_variant, roster_variant)
                    first_score = max(first_score, score)

            # Strategy 2: Zoom first name is prefix of roster first name (e.g., "Selene" vs "Selene Lizeth")
            if first_score < threshold and roster_first.startswith(norm_first + " "):
                first_score = 85  # Good partial match

            # Strategy 3: Roster first name is prefix of Zoom first name
            if first_score < threshold and norm_first.startswith(roster_first + " "):
                first_score = 85

            # Strategy 4: Two-word first name - Zoom split "Van Daisy" as first="Van", last="Daisy ..."
            # Check if combined Zoom name starts with roster first name (handles "Van Daisy" in roster)
            if first_score < threshold:
                # Try matching combined_zoom to roster_first (two-word first names)
                if roster_first and len(roster_first) > 3:
                    # Check if combined zoom name matches roster first name
                    combined_score = fuzz.ratio(combined_zoom.split()[0] if combined_zoom else "", roster_first.split()[0] if roster_first else "")
                    if combined_score >= 80:
                        # Check if the rest matches too
                        if len(combined_zoom.split()) > 1 and len(roster_first.split()) > 1:
                            rest_score = fuzz.ratio(" ".join(combined_zoom.split()[1:]), " ".join(roster_first.split()[1:]))
                            if rest_score >= 70:
                                first_score = 90

                # Direct comparison of combined zoom to roster full name
                full_match_score = fuzz.ratio(combined_zoom, roster_full)
                if full_match_score >= threshold:
                    first_score = max(first_score, full_match_score)

            # Strategy 5: Check if roster first name is multi-word and matches combined zoom
            if first_score < threshold and " " in roster_first:
                # Roster has two-word first name like "Van Daisy"
                roster_first_score = fuzz.ratio(combined_zoom, roster_first)
                if roster_first_score >= threshold:
                    first_score = roster_first_score

            # If first name is a strong match, check last name
            if first_score >= threshold:
                # Last name matching strategies
                last_score = 0

                if not norm_last:
                    # No last name provided - rely on first name only
                    last_score = 50  # Partial credit
                elif len(norm_last) == 1:
                    # Zoom has single character initial - check if roster last name starts with it
                    if roster_last and roster_last[0] == norm_last:
                        last_score = 90
                elif len(roster_last) == 1:
                    # Roster has single character initial - check if Zoom last name starts with it
                    if norm_last and norm_last[0] == roster_last:
                        last_score = 90
                else:
                    # Both have full last names - fuzzy match
                    last_score = fuzz.ratio(norm_last, roster_last)

                # Combined score (weighted toward first name)
                combined_score = (first_score * 0.6) + (last_score * 0.4)

                if combined_score > best_score:
                    best_score = combined_score
                    best_match = entry

            # Strategy 6: FALLBACK - try to match just first name if roster has multi-word first name
            # This handles cases where "Van Daisy" appears but normalized differently
            if best_score < threshold and " " in roster_first:
                # Check if any part of the roster first name matches norm_first or combined_zoom
                roster_parts = roster_first.split()
                for part in roster_parts:
                    if part == norm_first or fuzz.ratio(part, norm_first) >= 90:
                        # First name part matches, give high score
                        fallback_score = 75
                        if fallback_score > best_score:
                            best_score = fallback_score
                            best_match = entry
                            break

        # Only return if we have a good enough match
        if best_score >= threshold:
            print(f"[ROSTER] ✓ Matched '{first_name} {last_name}' -> '{best_match['first_name']} {best_match['last_name']}' (score: {best_score:.0f})", flush=True)
            return best_match

        # Lower threshold fallback for nickname matches
        if best_score >= 70 and best_match:
            print(f"[ROSTER] ✓ Matched '{first_name} {last_name}' -> '{best_match['first_name']} {best_match['last_name']}' (score: {best_score:.0f}, nickname fallback)", flush=True)
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

        # Detect column layout
        has_sid = (headers[0].strip().lower() in ["student id", "student id #", "id"]) if headers else False
        fn_col = 1 if has_sid else 0
        ln_col = 2 if has_sid else 1
        em_col = 3 if has_sid else 2
        data_start_col = 4 if has_sid else 3

        for row_idx, row in enumerate(data[1:], start=2):
            if not row or not any(row[:data_start_col]):
                continue

            profile = {
                "row_number": row_idx,
                "student_id": row[0] if has_sid and len(row) > 0 else "",
                "first_name": row[fn_col] if len(row) > fn_col else "",
                "last_name": row[ln_col] if len(row) > ln_col else "",
                "email": row[em_col] if len(row) > em_col else "",
                "attendance": {}
            }

            for col_idx, header in enumerate(headers[data_start_col:], start=data_start_col):
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
                                  participants: List[Dict], segments_data: Optional[List[Dict]] = None) -> Dict[str, Any]:
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
            segments_data: Optional list of segment data with time ranges and attendance per segment

        Returns:
            {"new_profiles": count, "updated_profiles": count, "profiles": [...]}
        """
        print(f"[SHEETS] Processing batch attendance for session {session_code}, {len(participants)} participants", flush=True)
        tab_name = f"Session {session_code}"

        # Step 1: Read existing data ONCE (bypass cache to get fresh data)
        data = self.get_tab_data(session_code, use_cache=False)
        headers = data[0] if data else ["Student ID", "First Name", "Last Name", "Email"]
        existing_rows = data[1:] if len(data) > 1 else []

        # Detect column layout - sheets may have Student ID in column A
        has_student_id_col = (headers[0].strip().lower() in ["student id", "student id #", "id"]) if headers else False
        if has_student_id_col:
            sid_col = 0
            fn_col = 1
            ln_col = 2
            email_col = 3
            print(f"[SHEETS] Detected Student ID column layout (A=ID, B=First, C=Last)", flush=True)
        else:
            sid_col = None
            fn_col = 0
            ln_col = 1
            email_col = 2
            print(f"[SHEETS] Using standard column layout (A=First, B=Last, C=Email)", flush=True)

        print(f"[SHEETS] Found {len(existing_rows)} existing profiles", flush=True)

        # Step 2: Build lookup index for existing profiles
        profile_index = {}  # (first_name_lower, last_name_lower) -> row_number
        email_index = {}    # email_lower -> row_number
        # Index for initial-based matching: (first_name_lower, last_initial) -> row_number
        initial_index = {}  # (first_name_lower, last_initial) -> row_number
        # Index for first-name-only matching (for Fritz, Dilorom_Russian, etc.)
        first_name_index = {}  # first_name_lower -> row_number (first occurrence)
        # ALSO build normalized indexes to match cleaned Zoom names
        norm_profile_index = {}  # (normalized_first, normalized_last) -> row_number
        norm_first_name_index = {}  # normalized_first -> row_number
        # Direct student ID -> row index from column A of the attendance sheet
        sheet_student_id_index = {}  # student_id_str -> row_number

        for row_idx, row in enumerate(existing_rows, start=2):
            min_cols = 3 if sid_col is None else 3
            if not row or len(row) < min_cols:
                continue
            # Skip completely empty rows
            check_end = 3 if sid_col is None else 4
            if not any(row[:check_end]):
                continue

            first_name = row[fn_col].strip().lower() if len(row) > fn_col and row[fn_col] else ""
            last_name = row[ln_col].strip().lower() if len(row) > ln_col and row[ln_col] else ""
            email = row[email_col].strip().lower() if len(row) > email_col and row[email_col] else ""

            # Build student ID index directly from column A
            if sid_col is not None:
                sheet_sid = row[sid_col].strip() if len(row) > sid_col and row[sid_col] else ""
                if sheet_sid:
                    sheet_student_id_index[sheet_sid] = row_idx

            # Also normalize for matching (handles "Dilorom_Russian" -> "dilorom", "Chrisnove's" -> "chrisnove")
            norm_first = self._normalize_name(first_name)
            norm_last = self._normalize_name(last_name)

            if first_name and last_name:
                profile_index[(first_name, last_name)] = row_idx
                # Also index by first name + last initial for initial-based matching
                if len(last_name) >= 1:
                    initial_index[(first_name, last_name[0])] = row_idx

            # Build normalized profile index
            if norm_first and norm_last:
                if (norm_first, norm_last) not in norm_profile_index:
                    norm_profile_index[(norm_first, norm_last)] = row_idx
                # Also index normalized first name + last initial
                if len(norm_last) >= 1 and (norm_first, norm_last[0]) not in initial_index:
                    initial_index[(norm_first, norm_last[0])] = row_idx

            # Index by first name only (keep first occurrence to prefer fuller names)
            if first_name and first_name not in first_name_index:
                first_name_index[first_name] = row_idx
            # Also index by normalized first name
            if norm_first and norm_first not in norm_first_name_index:
                norm_first_name_index[norm_first] = row_idx

            if email:
                email_index[email] = row_idx

        # Step 2b: Build student ID index from roster and sheet for ID-based matching
        roster_for_index = self.get_roster(session_code)
        student_id_index = {}  # student_id -> row_number (merged from sheet column A + roster cross-ref)
        roster_id_lookup = {}  # student_id -> {"first_name", "last_name", "student_id"}

        # First: use direct sheet column A mapping (most reliable - already in the sheet)
        student_id_index.update(sheet_student_id_index)

        for r in roster_for_index:
            sid = r.get("student_id", "").strip()
            if sid:
                roster_id_lookup[sid] = r
                # Also cross-reference by name if not already found via column A
                if sid not in student_id_index:
                    r_first = r["first_name"].strip().lower()
                    r_last = r["last_name"].strip().lower()
                    if (r_first, r_last) in profile_index:
                        student_id_index[sid] = profile_index[(r_first, r_last)]
                    elif (r_first, r_last) in norm_profile_index:
                        student_id_index[sid] = norm_profile_index[(r_first, r_last)]

        print(f"[SHEETS] Student ID index: {len(sheet_student_id_index)} from sheet col A, {len(roster_id_lookup)} roster IDs, {len(student_id_index)} total matched to rows", flush=True)

        # Step 3: Find or add date columns (including segment columns if provided)
        attendance_header = f"{date_str} Attendance"
        participation_header = f"{date_str} Participation"

        attendance_col = None
        participation_col = None
        segment_cols = {}  # segment_num -> column_index

        for idx, header in enumerate(headers):
            if header == attendance_header:
                attendance_col = idx
            elif header == participation_header:
                participation_col = idx
            # Check for existing segment columns
            elif segments_data:
                for seg in segments_data:
                    seg_header = f"{date_str} Seg{seg['segment_num']} ({seg['time_range']})"
                    if header == seg_header:
                        segment_cols[seg['segment_num']] = idx

        headers_changed = False
        if attendance_col is None:
            attendance_col = len(headers)
            headers.append(attendance_header)
            headers_changed = True

        if participation_col is None:
            participation_col = len(headers)
            headers.append(participation_header)
            headers_changed = True

        # Add segment columns if provided
        if segments_data:
            for seg in segments_data:
                seg_num = seg['segment_num']
                if seg_num not in segment_cols:
                    seg_header = f"{date_str} Seg{seg_num} ({seg['time_range']})"
                    segment_cols[seg_num] = len(headers)
                    headers.append(seg_header)
                    headers_changed = True

        # Update headers if needed (ONE API call)
        if headers_changed:
            cols_to_add = [attendance_header, participation_header]
            if segments_data:
                for seg in segments_data:
                    cols_to_add.append(f"{date_str} Seg{seg['segment_num']} ({seg['time_range']})")
            print(f"[SHEETS] Adding date columns: {', '.join(cols_to_add)}", flush=True)
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!1:1",
                valueInputOption="RAW",
                body={"values": [headers]}
            ).execute()

        # Step 4: Load name mappings and roster for matching info (NOT for replacing names)
        mappings = self.get_name_mappings(session_code)
        roster = self.get_roster(session_code)

        # Step 5: Categorize participants as new or existing
        new_profiles = []
        existing_updates = []
        roster_match_updates = []  # Updates for the Roster Match column
        results = {"new_profiles": 0, "updated_profiles": 0, "id_matched": 0, "roster_matched": 0, "mapping_matched": 0, "unmatched": 0, "needs_review": 0, "profiles": []}
        next_row = len(data) + 1  # Next available row

        for p in participants:
            zoom_first = p["first_name"].strip()
            zoom_last = p["last_name"].strip()
            email = p.get("email", "").strip()
            zoom_full_name = f"{zoom_first} {zoom_last}".strip()
            attendance_minutes = p["total_duration"] // 60

            # Skip generic "Zoom User" entries unless they have an email
            # These are users who haven't set their display name
            if zoom_full_name.lower() in ["zoom user", "zoom", "user", "guest", "iphone", "ipad"]:
                if not email:
                    print(f"[SHEETS] ⚠️ SKIPPING generic participant '{zoom_full_name}' (no email, cannot identify)", flush=True)
                    continue
                else:
                    print(f"[SHEETS] ⚠️ Generic name '{zoom_full_name}' but has email {email} - will try to match by email", flush=True)

            # Normalize Zoom name for matching (strip device suffixes, language tags, etc.)
            norm_first = self._normalize_name(zoom_first)
            norm_last = self._normalize_name(zoom_last)

            # Find roster match (for info only, NOT replacing the name)
            roster_match = None
            roster_match_str = ""
            match_confidence = "none"

            # === PRIMARY: Student ID matching (students must have 5-digit ID in Zoom name) ===
            student_id = self._extract_student_id(zoom_full_name)
            id_matched = False
            if student_id and student_id in roster_id_lookup:
                roster_entry = roster_id_lookup[student_id]
                roster_match_str = f"ID:{student_id} → {roster_entry['first_name']} {roster_entry['last_name']}"
                match_confidence = "high"
                id_matched = True
                results["id_matched"] += 1
                print(f"[SHEETS] ✓ Student ID match: '{zoom_full_name}' -> ID {student_id} -> {roster_entry['first_name']} {roster_entry['last_name']}", flush=True)
            elif student_id:
                # ID found in name but not in roster - flag for review
                print(f"[SHEETS] ⚠️ Student ID {student_id} found in '{zoom_full_name}' but NOT in roster", flush=True)

            # === FALLBACK: Name-based matching (if no student ID match) ===
            mapping_match = None
            if not id_matched:
                # Check for explicit name mappings
                mapping_match = self.find_mapping_for_name(zoom_full_name, session_code)
                if mapping_match:
                    roster_match_str = f"{mapping_match['first_name']} {mapping_match['last_name']}"
                    match_confidence = "high"
                    results["mapping_matched"] += 1
                    print(f"[MAPPINGS] ✓ Used mapping: '{zoom_full_name}' -> '{roster_match_str}'", flush=True)
                elif roster:
                    # Fall back to fuzzy roster matching
                    roster_match = self.match_to_roster(zoom_first, zoom_last, roster)
                    if roster_match:
                        roster_match_str = f"{roster_match['first_name']} {roster_match['last_name']}"
                        match_confidence = "high"
                        results["roster_matched"] += 1

            # === ROW LOOKUP: Find the existing row in the attendance sheet ===
            row_number = None

            # Priority 1: Student ID -> row (most reliable)
            if student_id and student_id in student_id_index:
                row_number = student_id_index[student_id]
                print(f"[SHEETS] ✓ Row found via student ID {student_id} -> row {row_number}", flush=True)

            # Priority 2: If ID matched a roster entry, find row by roster name
            if row_number is None and id_matched:
                roster_entry = roster_id_lookup[student_id]
                id_name_key = (roster_entry["first_name"].strip().lower(), roster_entry["last_name"].strip().lower())
                if id_name_key in profile_index:
                    row_number = profile_index[id_name_key]
                elif id_name_key in norm_profile_index:
                    row_number = norm_profile_index[id_name_key]
                if row_number:
                    # Cache this mapping for future participants in same batch
                    student_id_index[student_id] = row_number
                    print(f"[SHEETS] ✓ Row found via roster name for ID {student_id} -> row {row_number}", flush=True)

            # Priority 3: Check by email
            if row_number is None and email and email.lower() in email_index:
                row_number = email_index[email.lower()]

            # Priority 4: Try original (non-normalized) Zoom name in raw index
            if row_number is None:
                orig_key = (zoom_first.lower(), zoom_last.lower())
                if orig_key in profile_index:
                    row_number = profile_index[orig_key]

            # Priority 5: Try normalized Zoom name in normalized index
            if row_number is None:
                norm_key = (norm_first.lower(), norm_last.lower()) if norm_first and norm_last else None
                if norm_key and norm_key in norm_profile_index:
                    row_number = norm_profile_index[norm_key]

            # Priority 6: Try normalized name in raw profile index
            if row_number is None and norm_first:
                name_key = (norm_first.lower(), norm_last.lower() if norm_last else "")
                if name_key in profile_index:
                    row_number = profile_index[name_key]

            # Priority 7: If roster match found, try to find existing profile with roster name
            if row_number is None and roster_match:
                roster_key = (roster_match["first_name"].lower(), roster_match["last_name"].lower())
                if roster_key in profile_index:
                    row_number = profile_index[roster_key]
                if row_number is None and roster_key in norm_profile_index:
                    row_number = norm_profile_index[roster_key]

            # Priority 8: Initial-based matching (e.g., "Jamie R" matches existing "Jamie Reisman")
            if row_number is None and roster_match and len(roster_match["last_name"]) == 1:
                initial_key = (roster_match["first_name"].lower(), roster_match["last_name"].lower())
                if initial_key in initial_index:
                    row_number = initial_index[initial_key]
                    print(f"[SHEETS] ✓ Initial match: '{roster_match['first_name']} {roster_match['last_name']}' found existing profile at row {row_number}", flush=True)

            # Priority 9 (LAST RESORT): First-name-only matching
            if row_number is None and norm_first:
                if norm_first in norm_first_name_index:
                    row_number = norm_first_name_index[norm_first]
                    if match_confidence == "none":
                        match_confidence = "low"
                        roster_match_str = f"⚠️ REVIEW: matched by first name '{norm_first}' only"
                        results["needs_review"] += 1
                    print(f"[SHEETS] ✓ First-name match: '{zoom_full_name}' -> normalized '{norm_first}' found at row {row_number} (NEEDS REVIEW)", flush=True)
                # Also try raw first name index
                elif norm_first in first_name_index:
                    row_number = first_name_index[norm_first]
                    if match_confidence == "none":
                        match_confidence = "low"
                        roster_match_str = f"⚠️ REVIEW: matched by first name '{norm_first}' only"
                        results["needs_review"] += 1
                    print(f"[SHEETS] ✓ First-name match: '{zoom_full_name}' found existing profile at row {row_number} (NEEDS REVIEW)", flush=True)

            if row_number:
                # Existing profile - queue attendance update
                existing_updates.append({
                    "row": row_number,
                    "col": attendance_col,
                    "value": attendance_minutes,
                    "participant_name": zoom_full_name  # Track for debugging
                })
                # Also update Roster Match column if we have new match info
                if roster_match_str:
                    roster_match_updates.append({
                        "row": row_number,
                        "value": roster_match_str
                    })
                results["updated_profiles"] += 1
                results["profiles"].append({
                    "row": row_number,
                    "name": zoom_full_name,
                    "roster_match": roster_match_str,
                    "match_confidence": match_confidence,
                    "email": email,
                    "attendance_minutes": attendance_minutes,
                    "is_new": False,
                    "id_matched": id_matched,
                    "roster_matched": roster_match is not None,
                    "mapping_matched": mapping_match is not None
                })
                print(f"[SHEETS] ✓ Matched '{zoom_full_name}' ({attendance_minutes} min) to existing row {row_number}{' (via ID)' if id_matched else ''}", flush=True)
            else:
                # New profile - use ORIGINAL Zoom name (not roster name)
                # Include student ID in column A if the sheet has that layout
                if has_student_id_col:
                    # Use matched student ID, or the one extracted from the Zoom name
                    profile_sid = student_id if student_id else ""
                    new_profiles.append([profile_sid, zoom_first, zoom_last, email, roster_match_str])
                else:
                    new_profiles.append([zoom_first, zoom_last, email, roster_match_str])

                # Track student ID -> row for new profiles too
                if student_id and student_id not in student_id_index:
                    student_id_index[student_id] = next_row

                # Track for duplicate detection using BOTH raw and normalized names
                # Raw name (as stored in sheet)
                raw_key = (zoom_first.lower(), zoom_last.lower())
                profile_index[raw_key] = next_row

                # Normalized name (for matching variations like "Dilorom_Russian" -> "dilorom")
                if norm_first:
                    norm_key = (norm_first.lower(), norm_last.lower() if norm_last else "")
                    if norm_key not in norm_profile_index:
                        norm_profile_index[norm_key] = next_row
                    # Also track by normalized first name for first-name-only matching
                    if norm_first not in norm_first_name_index:
                        norm_first_name_index[norm_first] = next_row

                # Track the row number for this new profile
                row_number = next_row
                next_row += 1

                # Also queue attendance update for this new row
                existing_updates.append({
                    "row": row_number,
                    "col": attendance_col,
                    "value": attendance_minutes,
                    "participant_name": zoom_full_name  # Track for debugging
                })

                results["new_profiles"] += 1
                if not roster_match and not mapping_match and not id_matched:
                    results["unmatched"] += 1
                results["profiles"].append({
                    "row": row_number,
                    "name": zoom_full_name,
                    "roster_match": roster_match_str,
                    "match_confidence": match_confidence,
                    "email": email,
                    "attendance_minutes": attendance_minutes,
                    "is_new": True,
                    "id_matched": id_matched,
                    "roster_matched": roster_match is not None,
                    "mapping_matched": mapping_match is not None
                })
                print(f"[SHEETS] + Adding new profile '{zoom_full_name}' ({attendance_minutes} min) at row {row_number}", flush=True)

        # Step 6: Batch add all new profiles (ONE API call)
        if new_profiles:
            append_range = f"'{tab_name}'!A:E" if has_student_id_col else f"'{tab_name}'!A:D"
            print(f"[SHEETS] Adding {len(new_profiles)} new profiles in batch (range={append_range})", flush=True)
            self.sheets.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=append_range,
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": new_profiles}
            ).execute()

        # Step 7: Batch update all attendance values and roster matches (ONE API call)
        batch_data = []

        # CRITICAL BUG FIX: Detect duplicate row assignments and consolidate them
        # Multiple participants might match to the same row (e.g., "Jose Rafael ramirez" and "Jose Rafael Ramirez SPANISH")
        # If we write both values, the last one overwrites the first, causing incorrect attendance
        row_attendance = {}  # row -> total_attendance
        row_participants = {}  # row -> list of participant names (for debugging)
        row_segments = {}  # row -> {segment_num -> attendance_minutes}

        for update in existing_updates:
            row = update["row"]
            value = update["value"]
            participant_name = update.get("participant_name", "Unknown")

            if row in row_attendance:
                # DUPLICATE ROW DETECTED - this is the bug!
                print(f"[SHEETS] ⚠️ DUPLICATE ROW DETECTED: Row {row} has multiple participants!", flush=True)
                print(f"[SHEETS]   Existing participants: {row_participants[row]}", flush=True)
                print(f"[SHEETS]   New participant: '{participant_name}' with {value} minutes", flush=True)
                print(f"[SHEETS]   Previous total: {row_attendance[row]} minutes", flush=True)
                # Sum the values (these are likely the same person joining multiple times with different names)
                row_attendance[row] += value
                row_participants[row].append(participant_name)
                print(f"[SHEETS]   Consolidated value: {row_attendance[row]} minutes", flush=True)
            else:
                row_attendance[row] = value
                row_participants[row] = [participant_name]

            # Initialize segment attendance for this row if needed
            if segments_data and row not in row_segments:
                row_segments[row] = {seg['segment_num']: 0 for seg in segments_data}

        # Calculate segment attendance for each row
        if segments_data:
            for update in existing_updates:
                row = update["row"]
                participant_name = update.get("participant_name", "Unknown")

                # Find the participant key from the attendance data
                participant_key = None
                for p in participants:
                    zoom_full_name = f"{p['first_name']} {p['last_name']}".strip()
                    if zoom_full_name == participant_name:
                        participant_key = p.get("email") or zoom_full_name
                        break

                # Add segment attendance for this participant
                if participant_key:
                    for seg in segments_data:
                        seg_mins = seg['attendance'].get(participant_key, 0)
                        if seg_mins > 0:
                            row_segments[row][seg['segment_num']] += seg_mins

        # Add consolidated attendance updates
        for row, total_value in row_attendance.items():
            col_letter = self._col_index_to_letter(attendance_col)
            batch_data.append({
                "range": f"'{tab_name}'!{col_letter}{row}",
                "values": [[total_value]]
            })
            print(f"[SHEETS] Writing row {row}: {total_value} minutes (participants: {', '.join(row_participants[row])})", flush=True)

        # Add segment attendance updates
        if segments_data:
            for row in row_attendance.keys():
                for seg_num, seg_mins in row_segments.get(row, {}).items():
                    col_idx = segment_cols.get(seg_num)
                    if col_idx is not None:
                        col_letter = self._col_index_to_letter(col_idx)
                        batch_data.append({
                            "range": f"'{tab_name}'!{col_letter}{row}",
                            "values": [[seg_mins]]
                        })
                        print(f"[SHEETS] Writing row {row} segment {seg_num}: {seg_mins} minutes", flush=True)

        # Add roster match updates (column E if Student ID col exists, else column D)
        roster_match_col_letter = "E" if has_student_id_col else "D"
        for update in roster_match_updates:
            batch_data.append({
                "range": f"'{tab_name}'!{roster_match_col_letter}{update['row']}",
                "values": [[update["value"]]]
            })

        if batch_data:
            print(f"[SHEETS] Updating {len(row_attendance)} unique rows (consolidated from {len(existing_updates)} participant updates) and {len(roster_match_updates)} roster matches in batch", flush=True)
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": batch_data}
            ).execute()

        # Invalidate cache since we modified data
        self.invalidate_cache(session_code)

        print(f"[SHEETS] Batch complete: {results['new_profiles']} new, {results['updated_profiles']} updated, {results['roster_matched']} roster-matched, {results['mapping_matched']} mapping-matched, {results['unmatched']} unmatched, {results['needs_review']} needs review", flush=True)
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

    # ==================== SUMMARY TAB METHODS ====================

    def _get_summary_tab_name(self, session_code: str) -> str:
        """Get the name for a session's summary tab"""
        return f"Session {session_code} Summary"

    def find_summary_tab(self, session_code: str) -> Optional[Dict[str, Any]]:
        """Find the summary tab for a session"""
        tab_name = self._get_summary_tab_name(session_code)
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

    def create_summary_tab(self, session_code: str) -> Dict[str, Any]:
        """Create the summary tab for a session"""
        tab_name = self._get_summary_tab_name(session_code)

        try:
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
            headers = [["Student ID", "First Name", "Last Name", "Known Zoom Names"]]
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A1:D1",
                valueInputOption="RAW",
                body={"values": headers}
            ).execute()

            self._sheet_id_cache[tab_name] = sheet_id

            return {
                "name": tab_name,
                "sheet_id": sheet_id,
                "session_code": session_code
            }

        except HttpError as e:
            print(f"[SUMMARY] Error creating summary tab: {e}", flush=True)
            raise

    def get_or_create_summary_tab(self, session_code: str) -> Dict[str, Any]:
        """Find existing summary tab or create new one"""
        existing = self.find_summary_tab(session_code)
        if existing:
            return existing
        return self.create_summary_tab(session_code)

    def generate_session_summary(self, session_code: str) -> Dict[str, Any]:
        """
        Generate/update the Summary tab for a session.

        IMPORTANT: Uses ROSTER as the base truth - ALL roster students appear.
        Then matches attendance data from raw tab to roster students.
        Unmatched Zoom names appear at the bottom for review.

        Returns:
            {"students": count, "dates": [...], "summary_tab": tab_info}
        """
        print(f"[SUMMARY] Generating summary for session {session_code}", flush=True)

        # Step 1: Load roster FIRST - this is the source of truth
        roster = self.get_roster(session_code)
        print(f"[SUMMARY] Loaded {len(roster)} students from roster", flush=True)

        # Step 2: Read raw attendance data
        raw_data = self.get_tab_data(session_code, use_cache=False)
        raw_headers = raw_data[0] if raw_data else ["Student ID", "First Name", "Last Name", "Email", "Roster Match"]
        raw_rows = raw_data[1:] if len(raw_data) > 1 else []

        # Detect column layout
        raw_has_sid = (raw_headers[0].strip().lower() in ["student id", "student id #", "id"]) if raw_headers else False
        raw_fn_col = 1 if raw_has_sid else 0
        raw_ln_col = 2 if raw_has_sid else 1
        raw_rm_col = 4 if raw_has_sid else 3  # Roster Match column

        # Step 3: Extract date columns from raw headers
        date_columns = []
        for idx, header in enumerate(raw_headers):
            if header.endswith(" Attendance"):
                date_str = header.replace(" Attendance", "")
                date_columns.append({"date": date_str, "col_idx": idx})

        print(f"[SUMMARY] Found {len(date_columns)} attendance dates", flush=True)

        # Step 4: Initialize summary data with ALL roster students (even those with no attendance)
        # Key = "first_name last_name" lowercase
        summary_data = {}
        # Also build a student_id -> key lookup for ID-based roster match strings
        summary_sid_lookup = {}
        for entry in roster:
            key = f"{entry['first_name']} {entry['last_name']}".lower().strip()
            summary_data[key] = {
                "roster_info": entry,
                "canonical_name": f"{entry['first_name']} {entry['last_name']}",
                "zoom_names": set(),
                "attendance": {dc["date"]: 0 for dc in date_columns},  # Initialize all dates to 0
                "is_roster": True
            }
            if entry.get("student_id"):
                summary_sid_lookup[entry["student_id"]] = key

        # Step 5: Process raw attendance data and match to roster
        unmatched_entries = {}  # For Zoom names that don't match any roster student

        for row in raw_rows:
            min_cols = 3 if not raw_has_sid else 3
            if not row or len(row) < min_cols:
                continue

            zoom_first = row[raw_fn_col].strip() if len(row) > raw_fn_col and row[raw_fn_col] else ""
            zoom_last = row[raw_ln_col].strip() if len(row) > raw_ln_col and row[raw_ln_col] else ""
            roster_match = row[raw_rm_col].strip() if len(row) > raw_rm_col and row[raw_rm_col] else ""
            row_sid = row[0].strip() if raw_has_sid and len(row) > 0 and row[0] else ""
            zoom_full = f"{zoom_first} {zoom_last}".strip()

            # Skip empty names or generic Zoom User entries
            if not zoom_full or zoom_full.lower() in ["zoom user", "zoom", "user", "guest"]:
                continue

            # Determine if this row has a valid roster match
            is_matched = bool(roster_match and not roster_match.startswith("⚠️"))

            # Also try matching by student ID from column A
            if not is_matched and row_sid and row_sid in summary_sid_lookup:
                roster_key = summary_sid_lookup[row_sid]
                is_matched = True

            if is_matched:
                # Find the roster student by name
                # Handle "ID:12345 → First Last" format from ID matching
                roster_match_name = roster_match
                if roster_match.startswith("ID:") and "→" in roster_match:
                    roster_match_name = roster_match.split("→", 1)[1].strip()

                roster_key = roster_match_name.lower().strip() if roster_match_name else ""

                # Try direct student ID lookup first
                if row_sid and row_sid in summary_sid_lookup:
                    roster_key = summary_sid_lookup[row_sid]
                elif roster_key not in summary_data:
                    # Try parsing the roster_match as "First Last"
                    for key in summary_data.keys():
                        if key == roster_key or summary_data[key]["canonical_name"].lower() == roster_key:
                            roster_key = key
                            break

                if roster_key in summary_data:
                    # Add Zoom name to known names
                    summary_data[roster_key]["zoom_names"].add(zoom_full)

                    # Aggregate attendance (take max for each date)
                    for date_col in date_columns:
                        date_str = date_col["date"]
                        col_idx = date_col["col_idx"]
                        if col_idx < len(row) and row[col_idx]:
                            try:
                                minutes = int(float(row[col_idx]))
                                current_max = summary_data[roster_key]["attendance"].get(date_str, 0)
                                summary_data[roster_key]["attendance"][date_str] = max(current_max, minutes)
                            except ValueError:
                                pass
                else:
                    print(f"[SUMMARY] ⚠️ Roster match '{roster_match}' not found in roster!", flush=True)
                    # Treat as unmatched
                    is_matched = False

            if not is_matched:
                # Unmatched entry - track separately
                unmatched_key = f"__unmatched__{zoom_full.lower()}"
                if unmatched_key not in unmatched_entries:
                    unmatched_entries[unmatched_key] = {
                        "roster_info": {},
                        "canonical_name": zoom_full,
                        "zoom_names": set(),
                        "attendance": {dc["date"]: 0 for dc in date_columns},
                        "is_roster": False
                    }

                unmatched_entries[unmatched_key]["zoom_names"].add(zoom_full)

                # Aggregate attendance
                for date_col in date_columns:
                    date_str = date_col["date"]
                    col_idx = date_col["col_idx"]
                    if col_idx < len(row) and row[col_idx]:
                        try:
                            minutes = int(float(row[col_idx]))
                            current_max = unmatched_entries[unmatched_key]["attendance"].get(date_str, 0)
                            unmatched_entries[unmatched_key]["attendance"][date_str] = max(current_max, minutes)
                        except ValueError:
                            pass

        roster_with_attendance = sum(1 for d in summary_data.values() if any(v > 0 for v in d["attendance"].values()))
        print(f"[SUMMARY] {len(summary_data)} roster students ({roster_with_attendance} with attendance), {len(unmatched_entries)} unmatched", flush=True)

        # Step 6: Get or create summary tab
        summary_tab = self.get_or_create_summary_tab(session_code)
        tab_name = summary_tab["name"]

        # Step 7: Build summary headers and rows
        summary_headers = ["Student ID", "First Name", "Last Name", "Known Zoom Names"]
        for date_col in date_columns:
            summary_headers.append(f"{date_col['date']} Attendance")

        summary_rows = [summary_headers]

        # Add ALL roster students first (sorted by student ID)
        sorted_roster_keys = sorted(summary_data.keys(), key=lambda k: summary_data[k]["roster_info"].get("student_id", "zzz"))

        for key in sorted_roster_keys:
            data = summary_data[key]
            roster_info = data["roster_info"]
            zoom_names = sorted(data["zoom_names"])

            student_id = roster_info.get("student_id", "")
            first_name = roster_info.get("first_name", "")
            last_name = roster_info.get("last_name", "")

            # Build row
            row = [student_id, first_name, last_name, ", ".join(zoom_names)]
            for date_col in date_columns:
                date_str = date_col["date"]
                row.append(data["attendance"].get(date_str, 0))

            summary_rows.append(row)

        # Add unmatched entries at the bottom (sorted alphabetically)
        sorted_unmatched_keys = sorted(unmatched_entries.keys())

        for key in sorted_unmatched_keys:
            data = unmatched_entries[key]
            zoom_names = sorted(data["zoom_names"])
            canonical_name = data["canonical_name"]

            # Parse name
            name_parts = canonical_name.split(" ", 1)
            first_name = name_parts[0] if name_parts else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""

            # Build row with UNMATCHED marker
            row = ["UNMATCHED", first_name, last_name, ", ".join(zoom_names)]
            for date_col in date_columns:
                date_str = date_col["date"]
                row.append(data["attendance"].get(date_str, 0))

            summary_rows.append(row)

        # Step 7: Clear and write summary data
        # First, clear existing data
        sheet_id = summary_tab["sheet_id"]
        try:
            self.sheets.spreadsheets().values().clear(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A:ZZ"
            ).execute()
        except HttpError:
            pass  # Tab might be empty

        # Write all data at once
        if summary_rows:
            last_col = self._col_index_to_letter(len(summary_headers) - 1)
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A1:{last_col}{len(summary_rows)}",
                valueInputOption="RAW",
                body={"values": summary_rows}
            ).execute()

        print(f"[SUMMARY] Summary tab updated with {len(summary_rows) - 1} students", flush=True)

        return {
            "students": len(summary_rows) - 1,
            "dates": [dc["date"] for dc in date_columns],
            "summary_tab": summary_tab
        }

    def get_summary_data(self, session_code: str) -> Dict[str, Any]:
        """
        Get summary data for a session (used by student frontend).

        Returns:
            {"students": [...], "dates": [...], "total": count}
        """
        summary_tab = self.find_summary_tab(session_code)
        if not summary_tab:
            # Try to generate it
            result = self.generate_session_summary(session_code)
            if result["students"] == 0:
                return {"students": [], "dates": [], "total": 0}
            summary_tab = result["summary_tab"]

        tab_name = summary_tab["name"]

        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A:ZZ"
            ).execute()
            data = result.get("values", [])

            if not data or len(data) < 2:
                return {"students": [], "dates": [], "total": 0}

            headers = data[0]
            rows = data[1:]

            # Extract dates from headers (columns after "Known Zoom Names")
            dates = []
            for header in headers[4:]:
                if header.endswith(" Attendance"):
                    dates.append(header.replace(" Attendance", ""))

            # Parse student rows
            students = []
            for row_idx, row in enumerate(rows, start=2):
                if not row or not any(row[:3]):
                    continue

                student = {
                    "row_number": row_idx,
                    "student_id": row[0] if len(row) > 0 else "",
                    "first_name": row[1] if len(row) > 1 else "",
                    "last_name": row[2] if len(row) > 2 else "",
                    "known_zoom_names": row[3].split(", ") if len(row) > 3 and row[3] else [],
                    "attendance": {}
                }

                # Parse attendance dates
                for col_idx, header in enumerate(headers[4:], start=4):
                    if header.endswith(" Attendance"):
                        date_str = header.replace(" Attendance", "")
                        if col_idx < len(row) and row[col_idx]:
                            try:
                                student["attendance"][date_str] = int(float(row[col_idx]))
                            except ValueError:
                                student["attendance"][date_str] = 0
                        else:
                            student["attendance"][date_str] = 0

                students.append(student)

            return {
                "students": students,
                "dates": dates,
                "total": len(students)
            }

        except HttpError as e:
            print(f"[SUMMARY] Error reading summary data: {e}", flush=True)
            return {"students": [], "dates": [], "total": 0}

    # ==================== VIDEO PARTICIPATION METHODS ====================

    def get_or_create_video_participation_tab(self, session_code: str) -> Optional[Dict]:
        """Get or create Video Participation tab for a session."""
        tab_name = f"Video Participation {session_code}"

        # Check if tab exists
        tabs = self._get_all_tabs()
        for tab in tabs:
            if tab.get("properties", {}).get("title") == tab_name:
                return {
                    "name": tab_name,
                    "sheet_id": tab["properties"]["sheetId"],
                    "session_code": session_code
                }

        # Create new tab
        try:
            request = {
                "requests": [{
                    "addSheet": {
                        "properties": {
                            "title": tab_name
                        }
                    }
                }]
            }

            response = self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body=request
            ).execute()

            new_sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]

            # Add headers
            headers = [["Student Name"]]
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A1",
                valueInputOption="RAW",
                body={"values": headers}
            ).execute()

            print(f"[VIDEO] Created tab '{tab_name}'", flush=True)

            return {
                "name": tab_name,
                "sheet_id": new_sheet_id,
                "session_code": session_code
            }

        except HttpError as e:
            print(f"[VIDEO] Error creating tab: {e}", flush=True)
            return None

    def get_video_participation_data(self, session_code: str) -> List[List[str]]:
        """Get data from Video Participation tab."""
        tab_name = f"Video Participation {session_code}"

        try:
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A:ZZ"
            ).execute()

            return result.get("values", [["Student Name"]])

        except HttpError as e:
            print(f"[VIDEO] Error reading data: {e}", flush=True)
            return [["Student Name"]]

    def write_video_participation_data(self, session_code: str, data: List[List]) -> bool:
        """Write data to Video Participation tab."""
        tab_name = f"Video Participation {session_code}"

        try:
            # Clear existing data first
            self.sheets.spreadsheets().values().clear(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A:ZZ"
            ).execute()

            # Write new data
            self.sheets.spreadsheets().values().update(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{tab_name}'!A1",
                valueInputOption="RAW",
                body={"values": data}
            ).execute()

            print(f"[VIDEO] Wrote {len(data)} rows to '{tab_name}'", flush=True)
            return True

        except HttpError as e:
            print(f"[VIDEO] Error writing data: {e}", flush=True)
            return False


# Singleton instance
sheets_service = SheetsService()
