"""
Google Drive Service for Video Uploads

Handles uploading trimmed recordings to a shared Google Drive folder
with specific folder structure and permissions.
"""

import json
import os
import logging
from typing import Optional, Dict, Any, List
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


class DriveUploadError(Exception):
    """
    An upload failed for a reason a person can act on.

    The publish flow shows this text verbatim, so it says what went wrong and
    what to do about it — not "Drive upload failed", which sends you to the
    server logs to find out anything at all.
    """


def _reason_and_message(error: HttpError) -> tuple:
    """Pull Google's own reason code and message out of an HttpError."""
    reason, message = "", ""
    try:
        body = json.loads(error.content.decode("utf-8"))
        err = body.get("error", {})
        message = err.get("message", "") or ""
        errors = err.get("errors") or []
        if errors:
            reason = errors[0].get("reason", "") or ""
    except (ValueError, AttributeError, UnicodeDecodeError):
        pass
    return reason, message


def _explain_drive_error(error: HttpError, what: str) -> str:
    """
    Turn a Drive API error into a sentence that names the fix.

    Everything here is a failure we have actually hit or can plainly expect
    with a service account writing into a shared folder.
    """
    status = getattr(getattr(error, "resp", None), "status", 0)
    reason, message = _reason_and_message(error)
    account = os.getenv("GOOGLE_CLIENT_EMAIL", "the service account")

    if reason in ("storageQuotaExceeded", "quotaExceeded") and status == 403:
        return (
            f"Google Drive is out of storage, so {what} could not be saved. "
            f"Free up space in the shared drive (a few 3-hour recordings fill a "
            f"lot) or raise the storage limit, then send again."
        )
    if reason in ("insufficientFilePermissions", "forbidden") or status == 403:
        return (
            f"{account} is not allowed to write {what} into that Drive folder. "
            f"Give it Content manager access on the shared drive "
            f"(Manager if files need replacing). Google said: {message or 'permission denied'}"
        )
    if status == 404 or reason == "notFound":
        return (
            f"The Drive folder for {what} no longer exists, or is not shared "
            f"with {account}. Check the shared folder is still there and shared."
        )
    if status == 401:
        return (
            f"Google rejected the credentials while saving {what}. The service "
            f"account key may have been rotated or disabled."
        )
    if reason in ("userRateLimitExceeded", "rateLimitExceeded") or status == 429:
        return (
            f"Google is rate-limiting uploads, so {what} did not finish. "
            f"Wait a minute and send again."
        )
    if status >= 500:
        return (
            f"Google Drive had a server error while saving {what} and did not "
            f"recover after retrying. Sending again usually works."
        )
    return f"Drive rejected {what}: {message or error}"


class DriveService:
    """Service for uploading videos to Google Drive Shared Drives."""

    SCOPES = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]

    # Shared Drive folder ID for uploads
    SHARED_FOLDER_ID = "1k00chNZpP7rLOZvLE3rrMjIK_scVthaw"

    # Schedule spreadsheet for looking up day numbers
    SCHEDULE_SPREADSHEET_ID = "1CTpisCaJVUxZqrAShjAXM3cCueRiRHqBc3wAqmaXyeg"
    SCHEDULE_TAB_NAME = "All Official AALB Schedules (PASTE ROWS ONLY)"

    def __init__(self):
        self._drive_service = None
        self._sheets_service = None
        self._folder_cache = {}  # Cache for folder IDs
        self._schedule_cache = {}  # Cache for schedule data
        logger.info("DriveService initialized")

    def _get_credentials(self):
        """Get Google API credentials from environment variables or file."""
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
    def drive(self):
        """Get the Drive API service."""
        if not self._drive_service:
            credentials = self._get_credentials()
            self._drive_service = build("drive", "v3", credentials=credentials)
        return self._drive_service

    @property
    def sheets(self):
        """Get the Sheets API service for reading schedule."""
        if not self._sheets_service:
            credentials = self._get_credentials()
            self._sheets_service = build("sheets", "v4", credentials=credentials)
        return self._sheets_service

    def get_day_number(self, session_code: str, meeting_date: str) -> Optional[int]:
        """
        Look up the day number for a session and date from the schedule spreadsheet.

        Args:
            session_code: e.g., "127"
            meeting_date: e.g., "Nov10" or "11/10"

        Returns:
            Day number (0-based or 1-based depending on schedule) or None if not found
        """
        cache_key = f"{session_code}:{meeting_date}"
        if cache_key in self._schedule_cache:
            return self._schedule_cache[cache_key]

        logger.info(f"[DRIVE] Looking up day number for Session {session_code} on {meeting_date}")

        try:
            # First, get spreadsheet metadata to find available sheet names
            spreadsheet_meta = self.sheets.spreadsheets().get(
                spreadsheetId=self.SCHEDULE_SPREADSHEET_ID,
                fields="sheets.properties.title"
            ).execute()

            available_sheets = [s['properties']['title'] for s in spreadsheet_meta.get('sheets', [])]
            logger.info(f"[DRIVE] Available sheets in schedule spreadsheet: {available_sheets}")

            # Find the right sheet - try configured name first, then look for alternatives
            sheet_name = None
            if self.SCHEDULE_TAB_NAME in available_sheets:
                sheet_name = self.SCHEDULE_TAB_NAME
            else:
                # Try to find a sheet that might contain schedules
                for name in available_sheets:
                    name_lower = name.lower()
                    if 'schedule' in name_lower or 'session' in name_lower or session_code in name:
                        sheet_name = name
                        logger.info(f"[DRIVE] Using fallback sheet: {sheet_name}")
                        break

                # If still not found, use the first sheet
                if not sheet_name and available_sheets:
                    sheet_name = available_sheets[0]
                    logger.info(f"[DRIVE] Using first sheet as fallback: {sheet_name}")

            if not sheet_name:
                logger.error("[DRIVE] No sheets found in schedule spreadsheet")
                return None

            # Read the schedule spreadsheet
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.SCHEDULE_SPREADSHEET_ID,
                range=f"'{sheet_name}'!A:Z"
            ).execute()

            rows = result.get("values", [])
            if not rows:
                logger.warning("[DRIVE] Schedule spreadsheet is empty")
                return None

            headers = rows[0] if rows else []
            logger.info(f"[DRIVE] Schedule has {len(rows)} rows, {len(headers)} columns")
            logger.info(f"[DRIVE] Headers: {headers[:15]}")

            # Log first few rows to understand structure
            for i, row in enumerate(rows[:5]):
                logger.info(f"[DRIVE] Row {i}: {row[:10] if len(row) > 10 else row}")

            # SIMPLE APPROACH: Find all rows containing this session, count which one has our date
            # This works regardless of spreadsheet structure
            session_rows = []

            # Determine if this is a Saturday or Sunday based on the date
            target_day_of_week = self._get_day_of_week(meeting_date)
            logger.info(f"[DRIVE] Target date {meeting_date} is a {target_day_of_week or 'unknown day'}")

            for row_idx, row in enumerate(rows):
                row_text = ' '.join(str(cell) for cell in row).lower()
                # Check if this row mentions our session
                # Support various formats: "Session 129", "Sess 129", "S129", "129",
                # "Session 129 Sat", "Session 129 Sun", "Session 129 (Saturday)", etc.
                session_match = False

                # Check standard patterns
                if (f"session {session_code}".lower() in row_text or
                    f"sess {session_code}".lower() in row_text or
                    f" {session_code} " in f" {row_text} " or
                    f"s{session_code}".lower() in row_text or
                    f"session{session_code}".lower() in row_text):
                    session_match = True

                if session_match:
                    # Check if this row is day-specific (Saturday/Sunday/Both)
                    # Check for BOTH days first (e.g., "Saturdays and Sundays")
                    has_saturday = 'saturday' in row_text or ' sat ' in f" {row_text} " or row_text.endswith(' sat') or 'sat)' in row_text
                    has_sunday = 'sunday' in row_text or ' sun ' in f" {row_text} " or row_text.endswith(' sun') or 'sun)' in row_text

                    if has_saturday and has_sunday:
                        row_day = 'Both'  # Session runs on both Saturday and Sunday
                    elif has_saturday:
                        row_day = 'Saturday'
                    elif has_sunday:
                        row_day = 'Sunday'
                    else:
                        row_day = None

                    # Log what we found
                    first_cell = str(row[0])[:50] if row else ''
                    logger.info(f"[DRIVE] Row {row_idx} matches session {session_code}: '{first_cell}...' (day={row_day})")

                    # If we know the target day of week and this row has a day, only include if it matches
                    if target_day_of_week and row_day:
                        # "Both" matches any weekend day
                        if row_day == 'Both' or target_day_of_week == row_day:
                            session_rows.append((row_idx, row))
                            logger.info(f"[DRIVE] Including row {row_idx} - day matches ({row_day})")
                        else:
                            logger.info(f"[DRIVE] Skipping row {row_idx} - day mismatch (target={target_day_of_week}, row={row_day})")
                    else:
                        # No day filter, include all matching rows
                        session_rows.append((row_idx, row))

            logger.info(f"[DRIVE] Found {len(session_rows)} rows mentioning Session {session_code} (filtered for {target_day_of_week or 'any day'})")

            # Now find which row has our date and count its position
            day_counter = 0
            for row_idx, row in session_rows:
                row_text = ' '.join(str(cell) for cell in row)
                # Check each cell for date match
                for cell in row:
                    if self._dates_match(str(cell), meeting_date):
                        logger.info(f"[DRIVE] Found day {day_counter} - row {row_idx} contains date {meeting_date}")
                        self._schedule_cache[cache_key] = day_counter
                        return day_counter
                day_counter += 1

            logger.warning(f"[DRIVE] Could not find date {meeting_date} in any of the {len(session_rows)} session rows")

            # FALLBACK: If day filtering was applied but no date found, try without filtering
            # This handles cases where the date is in a different session variant row
            if target_day_of_week:
                logger.info(f"[DRIVE] Retrying without day-of-week filter...")
                all_session_rows = []
                for row_idx, row in enumerate(rows):
                    row_text = ' '.join(str(cell) for cell in row).lower()
                    if (f"session {session_code}".lower() in row_text or
                        f"sess {session_code}".lower() in row_text or
                        f" {session_code} " in f" {row_text} " or
                        f"s{session_code}".lower() in row_text or
                        f"session{session_code}".lower() in row_text):
                        all_session_rows.append((row_idx, row))

                logger.info(f"[DRIVE] Found {len(all_session_rows)} rows without day filter")

                day_counter = 0
                for row_idx, row in all_session_rows:
                    for cell in row:
                        if self._dates_match(str(cell), meeting_date):
                            logger.info(f"[DRIVE] Found day {day_counter} - row {row_idx} contains date {meeting_date} (no day filter)")
                            self._schedule_cache[cache_key] = day_counter
                            return day_counter
                    day_counter += 1

                logger.warning(f"[DRIVE] Still could not find date {meeting_date} even without day filter")

            # Fallback: Try original strategies

            # STRATEGY 1: Look for session code in headers (sessions as columns)
            session_col = None
            for idx, header in enumerate(headers):
                header_str = str(header).strip()
                # Try various formats: "Session 127", "127", "Sess 127", etc.
                if (f"Session {session_code}" in header_str or
                    f"Sess {session_code}" in header_str or
                    header_str == session_code or
                    header_str == f"S{session_code}"):
                    session_col = idx
                    logger.info(f"[DRIVE] Found session column at index {idx}: '{header_str}'")
                    break

            if session_col is not None:
                # Sessions are columns - look for date in rows
                # Count days for this session by finding matching dates
                day_counter = 0
                for row_idx, row in enumerate(rows[1:], start=1):
                    if not row or len(row) <= session_col:
                        continue

                    # Check if this row has a date that we can match
                    date_cell = row[0] if len(row) > 0 else ""
                    session_cell = row[session_col] if len(row) > session_col else ""

                    # If the session cell is not empty, this is a scheduled day
                    if session_cell and str(session_cell).strip():
                        if self._dates_match(date_cell, meeting_date):
                            self._schedule_cache[cache_key] = day_counter
                            logger.info(f"[DRIVE] Found day {day_counter} for session {session_code} on {meeting_date} (matched '{date_cell}')")
                            return day_counter
                        day_counter += 1

                logger.warning(f"[DRIVE] Session column found but date {meeting_date} not matched")

            # STRATEGY 2: Look for session in first column (sessions as rows)
            session_row = None
            for row_idx, row in enumerate(rows):
                if not row:
                    continue
                first_cell = str(row[0]).strip() if len(row) > 0 else ""
                if (f"Session {session_code}" in first_cell or
                    f"Sess {session_code}" in first_cell or
                    first_cell == session_code or
                    first_cell == f"S{session_code}"):
                    session_row = row_idx
                    logger.info(f"[DRIVE] Found session row at index {row_idx}: '{first_cell}'")
                    break

            if session_row is not None:
                # Session is a row - dates are in columns
                day_counter = 0
                for col_idx, cell in enumerate(rows[session_row][1:], start=1):
                    cell_str = str(cell).strip() if cell else ""
                    # Check if this column header has our date
                    header_cell = headers[col_idx] if col_idx < len(headers) else ""

                    if cell_str:  # Non-empty means this is a scheduled day
                        if self._dates_match(str(header_cell), meeting_date):
                            self._schedule_cache[cache_key] = day_counter
                            logger.info(f"[DRIVE] Found day {day_counter} for session {session_code} on {meeting_date}")
                            return day_counter
                        day_counter += 1

            # STRATEGY 3: Search entire spreadsheet for session + date combination
            logger.info(f"[DRIVE] Trying full spreadsheet search for Session {session_code}")
            for row_idx, row in enumerate(rows):
                for col_idx, cell in enumerate(row):
                    cell_str = str(cell).strip()
                    # Look for patterns like "Session 127" or just the session code
                    if f"Session {session_code}" in cell_str or cell_str == session_code:
                        # Found session mention - look for date in nearby cells
                        logger.info(f"[DRIVE] Found session mention at row {row_idx}, col {col_idx}: '{cell_str}'")

                        # Check if we can count days from here
                        # Look in the same row for dates
                        for nearby_col, nearby_cell in enumerate(row):
                            if self._dates_match(str(nearby_cell), meeting_date):
                                day_num = nearby_col - col_idx - 1 if nearby_col > col_idx else 0
                                if day_num >= 0:
                                    self._schedule_cache[cache_key] = day_num
                                    logger.info(f"[DRIVE] Found day {day_num} via nearby date match")
                                    return day_num

            logger.warning(f"[DRIVE] Could not find day number for session {session_code} on {meeting_date}")
            logger.info(f"[DRIVE] Sample data from spreadsheet - Row 0: {rows[0][:5] if rows else 'empty'}")
            logger.info(f"[DRIVE] Sample data from spreadsheet - Row 1: {rows[1][:5] if len(rows) > 1 else 'empty'}")
            return None

        except HttpError as e:
            logger.error(f"[DRIVE] Error reading schedule: {e}")
            return None

    def _dates_match(self, cell_date: str, target_date: str) -> bool:
        """Check if two date strings represent the same date."""
        import re

        if not cell_date or not target_date:
            return False

        # Extract month and day patterns
        # Handle formats like: Nov10, Nov 10, 11/10, November 10, 12/17/2024, Wed 12/17
        months = {
            'jan': '01', 'january': '01',
            'feb': '02', 'february': '02',
            'mar': '03', 'march': '03',
            'apr': '04', 'april': '04',
            'may': '05',
            'jun': '06', 'june': '06',
            'jul': '07', 'july': '07',
            'aug': '08', 'august': '08',
            'sep': '09', 'september': '09',
            'oct': '10', 'october': '10',
            'nov': '11', 'november': '11',
            'dec': '12', 'december': '12'
        }

        def normalize_date(d):
            if not d:
                return None
            d = str(d).lower().strip()

            # Try MM/DD/YYYY or MM/DD format
            match = re.search(r'(\d{1,2})/(\d{1,2})(?:/\d{2,4})?', d)
            if match:
                return f"{int(match.group(1)):02d}/{int(match.group(2)):02d}"

            # Try Month Day format (Nov10, Nov 10, November 10)
            for month_name, month_num in months.items():
                if month_name in d:
                    day_match = re.search(r'(\d{1,2})', d)
                    if day_match:
                        return f"{month_num}/{int(day_match.group(1)):02d}"

            # Try to extract any MM/DD pattern from a longer string
            # e.g., "Wed Dec 17" or "Wednesday, December 17"
            for month_name, month_num in months.items():
                if month_name in d:
                    # Look for day number after month name
                    pattern = month_name + r'[a-z]*\s*(\d{1,2})'
                    match = re.search(pattern, d)
                    if match:
                        return f"{month_num}/{int(match.group(1)):02d}"

            return None

        norm_cell = normalize_date(cell_date)
        norm_target = normalize_date(target_date)

        if norm_cell and norm_target:
            return norm_cell == norm_target

        return False

    def _get_day_of_week(self, date_str: str) -> Optional[str]:
        """
        Determine the day of week for a given date string.

        Args:
            date_str: Date in format "MM/DD", "Nov10", etc.

        Returns:
            "Saturday", "Sunday", or None if can't determine
        """
        import re
        from datetime import datetime

        months = {
            'jan': 1, 'january': 1, 'feb': 2, 'february': 2,
            'mar': 3, 'march': 3, 'apr': 4, 'april': 4,
            'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
            'aug': 8, 'august': 8, 'sep': 9, 'september': 9,
            'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
            'dec': 12, 'december': 12
        }

        try:
            date_lower = str(date_str).lower().strip()
            month = None
            day = None

            # Try MM/DD format
            match = re.search(r'(\d{1,2})/(\d{1,2})', date_lower)
            if match:
                month = int(match.group(1))
                day = int(match.group(2))
            else:
                # Try Month Day format (Nov10, Nov 10, November 10)
                for month_name, month_num in months.items():
                    if month_name in date_lower:
                        month = month_num
                        day_match = re.search(r'(\d{1,2})', date_lower)
                        if day_match:
                            day = int(day_match.group(1))
                        break

            if month and day:
                # Assume current year (2026 based on context)
                year = datetime.now().year
                date_obj = datetime(year, month, day)
                day_name = date_obj.strftime('%A')  # Full day name
                if day_name in ('Saturday', 'Sunday'):
                    return day_name
                # Also check previous year in case dates span year boundary
                try:
                    date_obj_prev = datetime(year - 1, month, day)
                    day_name_prev = date_obj_prev.strftime('%A')
                    if day_name_prev in ('Saturday', 'Sunday'):
                        return day_name_prev
                except ValueError:
                    pass
                return day_name

        except Exception as e:
            logger.warning(f"[DRIVE] Could not determine day of week for {date_str}: {e}")

        return None

    def get_or_create_session_folder(self, session_code: str) -> Optional[str]:
        """
        Get or create the session folder (e.g., "Session 127").

        Returns:
            Folder ID or None on error
        """
        folder_name = f"Session {session_code}"
        cache_key = f"session_{session_code}"

        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        try:
            # Check if folder exists
            query = f"name='{folder_name}' and '{self.SHARED_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = self.drive.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

            files = results.get('files', [])
            if files:
                folder_id = files[0]['id']
                self._folder_cache[cache_key] = folder_id
                logger.info(f"[DRIVE] Found existing folder: {folder_name}")
                return folder_id

            # Create folder
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [self.SHARED_FOLDER_ID]
            }

            folder = self.drive.files().create(
                body=file_metadata,
                fields='id',
                supportsAllDrives=True
            ).execute()

            folder_id = folder.get('id')
            self._folder_cache[cache_key] = folder_id
            logger.info(f"[DRIVE] Created folder: {folder_name}")
            return folder_id

        except HttpError as e:
            logger.error(f"[DRIVE] Error creating session folder: {e}")
            return None

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """
        Get or create a folder by name under a given parent.

        Generic version of the session/view helpers below, used by the publish
        flow so folder names come from the plan rather than being hard-coded to
        the "Session N / Speaker View" convention.

        Raises DriveUploadError rather than returning None, so the reason
        reaches the person waiting on the publish instead of only the logs.
        """
        cache_key = f"path_{parent_id}_{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        # Escape single quotes for the Drive query language.
        safe_name = name.replace("'", "\\'")
        try:
            query = (
                f"name='{safe_name}' and '{parent_id}' in parents "
                f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
            )
            results = self.drive.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

            files = results.get('files', [])
            if files:
                self._folder_cache[cache_key] = files[0]['id']
                return files[0]['id']

            folder = self.drive.files().create(
                body={
                    'name': name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [parent_id],
                },
                fields='id',
                supportsAllDrives=True
            ).execute()

            folder_id = folder.get('id')
            self._folder_cache[cache_key] = folder_id
            logger.info(f"[DRIVE] Created folder: {name}")
            return folder_id

        except HttpError as e:
            logger.error(f"[DRIVE] Error creating folder {name}: {e}")
            raise DriveUploadError(_explain_drive_error(e, f"the folder '{name}'")) from e

    def ensure_path(self, folder_names: List[str]) -> str:
        """
        Resolve a folder path under the shared folder, creating what's missing,
        and return the innermost folder's ID.

        Worth calling before a long trim: it is a couple of cheap API calls
        that prove we can actually write there, so a permissions or sharing
        problem surfaces in seconds instead of after twenty minutes of work.
        Results are cached, so the upload's own resolution costs nothing.
        """
        parent = self.SHARED_FOLDER_ID
        for name in folder_names:
            parent = self.get_or_create_folder(name, parent)
        return parent

    def _find_file(self, file_name: str, parent_id: str) -> Optional[Dict[str, Any]]:
        """The file of this name already in this folder, if there is one."""
        safe_name = file_name.replace("'", "\\'")
        found = self.drive.files().list(
            q=f"name='{safe_name}' and '{parent_id}' in parents and trashed=false",
            spaces='drive',
            fields='files(id, name)',
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute().get('files', [])
        return found[0] if found else None

    def upload_to_path(
        self,
        file_path: str,
        folder_names: List[str],
        file_name: str,
        progress_callback=None,
    ) -> Dict[str, Any]:
        """
        Upload a file to an arbitrary folder path under the shared folder,
        creating folders as needed.

        Re-publishing the same recording replaces the file *in place* — a new
        revision of the existing file rather than delete-then-create. Three
        reasons that matters:

          * deleting first means a failed upload leaves you with neither the
            old file nor the new one;
          * permanently deleting in a shared drive needs Manager rights, while
            updating only needs Content manager, so the delete was failing for
            exactly the files most likely to be re-sent;
          * the file ID survives, so anything already pointing at it — the
            Classroom attachment, a link someone shared — keeps working.

        Used by the publish flow, where both the path and the filename come
        from the class's own settings (or an Unsorted fallback when the
        recording isn't matched to a class).

        Raises DriveUploadError with a readable reason on failure.
        """
        if not os.path.exists(file_path):
            raise DriveUploadError(f"The trimmed file for {file_name} is missing.")
        size = os.path.getsize(file_path)
        if size == 0:
            raise DriveUploadError(
                f"The trimmed file for {file_name} is empty — the trim produced "
                f"nothing. Check the start and end times cover part of the recording."
            )

        parent = self.ensure_path(folder_names)

        try:
            existing = self._find_file(file_name, parent)

            media = MediaFileUpload(
                file_path,
                mimetype='video/mp4',
                resumable=True,
                chunksize=1024 * 1024 * 5,
            )

            if existing:
                logger.info(
                    f"[DRIVE] Replacing contents of existing file: {file_name} "
                    f"({existing['id']})"
                )
                request = self.drive.files().update(
                    fileId=existing['id'],
                    media_body=media,
                    fields='id, name, webViewLink',
                    supportsAllDrives=True
                )
            else:
                request = self.drive.files().create(
                    body={'name': file_name, 'parents': [parent]},
                    media_body=media,
                    fields='id, name, webViewLink',
                    supportsAllDrives=True
                )

            response = None
            while response is None:
                # num_retries makes the client ride out the 5xx and 429 blips
                # that a multi-gigabyte upload will otherwise hit sooner or later.
                status, response = request.next_chunk(num_retries=5)
                if status and progress_callback:
                    progress_callback(status.resumable_progress, status.total_size)

            file_id = response.get('id')
            self._set_file_permissions(file_id)
            logger.info(f"[DRIVE] Uploaded {file_name} ({file_id}, {size} bytes)")

            return {
                'file_id': file_id,
                'name': response.get('name', file_name),
                'web_view_link': response.get('webViewLink'),
                'folder_path': folder_names,
                'replaced': bool(existing),
            }

        except HttpError as e:
            logger.error(f"[DRIVE] Error uploading {file_name}: {e}")
            raise DriveUploadError(_explain_drive_error(e, file_name)) from e
        except OSError as e:
            # A dropped connection or a full disk mid-upload. Neither is an
            # HttpError, and both used to surface as a bare "upload failed".
            logger.error(f"[DRIVE] Transport error uploading {file_name}: {e}")
            raise DriveUploadError(
                f"The connection to Google Drive failed while uploading "
                f"{file_name} ({e}). Sending again resumes from scratch."
            ) from e

    def grant_access(self, file_id: str, email: str, role: str = "writer") -> bool:
        """
        Give a person direct access to a file we own.

        Needed before Classroom can attach it. Classroom posts *as the teacher*,
        and attaching with shareMode VIEW means Classroom tries to share the file
        on that teacher's behalf — which Google refuses with PERMISSION_DENIED if
        the teacher has no rights over a file the service account owns.

        Best-effort: the upload has already succeeded by this point, so a failure
        here is logged and reported, never fatal.
        """
        if not email:
            return False
        try:
            self.drive.permissions().create(
                fileId=file_id,
                body={"type": "user", "role": role, "emailAddress": email},
                sendNotificationEmail=False,
                supportsAllDrives=True,
            ).execute()
            logger.info(f"[DRIVE] Granted {role} on {file_id} to {email}")
            return True
        except HttpError as e:
            logger.warning(f"[DRIVE] Could not grant {role} on {file_id} to {email}: {e}")
            return False

    def get_or_create_view_folder(self, session_folder_id: str, view_type: str) -> Optional[str]:
        """
        Get or create a view type folder (Gallery View or Speaker View).

        Args:
            session_folder_id: Parent session folder ID
            view_type: "gallery" or "speaker"

        Returns:
            Folder ID or None on error
        """
        folder_name = "Gallery View" if view_type == "gallery" else "Speaker View"
        cache_key = f"view_{session_folder_id}_{view_type}"

        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        try:
            # Check if folder exists
            query = f"name='{folder_name}' and '{session_folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = self.drive.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

            files = results.get('files', [])
            if files:
                folder_id = files[0]['id']
                self._folder_cache[cache_key] = folder_id
                return folder_id

            # Create folder
            file_metadata = {
                'name': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [session_folder_id]
            }

            folder = self.drive.files().create(
                body=file_metadata,
                fields='id',
                supportsAllDrives=True
            ).execute()

            folder_id = folder.get('id')
            self._folder_cache[cache_key] = folder_id
            logger.info(f"[DRIVE] Created folder: {folder_name}")
            return folder_id

        except HttpError as e:
            logger.error(f"[DRIVE] Error creating view folder: {e}")
            return None

    def upload_video(
        self,
        file_path: str,
        session_code: str,
        day_number: int,
        meeting_date: str,
        view_type: str,
        progress_callback=None
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a video file to Google Drive with proper folder structure.

        Args:
            file_path: Path to the video file to upload
            session_code: e.g., "127"
            day_number: e.g., 0, 1, 2...
            meeting_date: e.g., "Nov10"
            view_type: "gallery" or "speaker"
            progress_callback: Optional callback(bytes_uploaded, total_bytes)

        Returns:
            {"file_id": ..., "web_view_link": ..., "name": ...} or None on error
        """
        try:
            # Create folder structure
            session_folder = self.get_or_create_session_folder(session_code)
            if not session_folder:
                return None

            view_folder = self.get_or_create_view_folder(session_folder, view_type)
            if not view_folder:
                return None

            # Build file name: "Session 127 - Day 0 - Nov10 (Speaker View).mp4"
            view_label = "Gallery View" if view_type == "gallery" else "Speaker View"
            file_name = f"Session {session_code} - Day {day_number} - {meeting_date} ({view_label}).mp4"

            # Check if file already exists
            query = f"name='{file_name}' and '{view_folder}' in parents and trashed=false"
            results = self.drive.files().list(
                q=query,
                spaces='drive',
                fields='files(id, name)',
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

            existing = results.get('files', [])
            if existing:
                # Delete existing file to replace
                logger.info(f"[DRIVE] Replacing existing file: {file_name}")
                self.drive.files().delete(
                    fileId=existing[0]['id'],
                    supportsAllDrives=True
                ).execute()

            # Upload file
            file_metadata = {
                'name': file_name,
                'parents': [view_folder]
            }

            media = MediaFileUpload(
                file_path,
                mimetype='video/mp4',
                resumable=True,
                chunksize=1024 * 1024 * 5  # 5MB chunks
            )

            logger.info(f"[DRIVE] Starting upload: {file_name}")

            request = self.drive.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, name, webViewLink',
                supportsAllDrives=True
            )

            response = None
            while response is None:
                status, response = request.next_chunk()
                if status and progress_callback:
                    progress_callback(status.resumable_progress, status.total_size)

            file_id = response.get('id')

            # Set permissions: viewable with link, not downloadable
            self._set_file_permissions(file_id)

            logger.info(f"[DRIVE] Upload complete: {file_name} (ID: {file_id})")

            return {
                'file_id': file_id,
                'name': file_name,
                'web_view_link': response.get('webViewLink'),
                'session_code': session_code,
                'day_number': day_number,
                'view_type': view_type
            }

        except HttpError as e:
            logger.error(f"[DRIVE] Error uploading video: {e}")
            return None

    def _set_file_permissions(self, file_id: str) -> bool:
        """
        Anyone with the link can watch it; downloading and copying are off.

        This is how students reach the recording — no AALB sign-in, no Classroom
        attachment, no per-student sharing. "anyone" is deliberate rather than
        domain-restricted, because a link that demands a sign-in is not a link
        anyone can watch.

        The download restriction is best-effort by nature: Drive has no hard
        "no downloads" switch, only copyRequiresWriterPermission, which hides
        the download and copy controls from viewers. Anyone determined can still
        capture the stream. Replacing this with a player that streams without
        exposing the file is the real fix, and the seam for it is here — swap
        what this returns a link to, and nothing upstream changes.
        """
        try:
            self.drive.permissions().create(
                fileId=file_id,
                body={'type': 'anyone', 'role': 'reader'},
                supportsAllDrives=True
            ).execute()

            try:
                self.drive.files().update(
                    fileId=file_id,
                    body={
                        'copyRequiresWriterPermission': True,
                        'viewersCanCopyContent': False
                    },
                    supportsAllDrives=True
                ).execute()
                logger.info(f"[DRIVE] {file_id}: link-viewable, download restricted")
            except HttpError as e:
                # Not fatal, but say which of the two properties is missing
                # rather than implying both are in place.
                logger.warning(
                    f"[DRIVE] {file_id}: link-viewable, but the download restriction "
                    f"was refused — viewers will be able to download it ({e})"
                )
            return True

        except HttpError as e:
            logger.error(f"[DRIVE] Error setting permissions: {e}")
            return False

    def get_scheduled_time(self, session_code: str, meeting_date: str) -> Optional[Dict[str, str]]:
        """
        Get the scheduled start and end time for a session on a specific date.

        Returns:
            {"start_time": "HH:MM", "end_time": "HH:MM", "duration_minutes": N} or None
        """
        try:
            # Read the schedule spreadsheet
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.SCHEDULE_SPREADSHEET_ID,
                range=f"'{self.SCHEDULE_TAB_NAME}'!A:Z"
            ).execute()

            rows = result.get("values", [])
            if not rows:
                return None

            # Try to find session/date combination and extract time info
            # This depends on the spreadsheet structure
            # For now, return None - this would need to be customized
            # based on the actual schedule spreadsheet format

            logger.warning(f"[DRIVE] Schedule time lookup not yet implemented for {session_code} on {meeting_date}")
            return None

        except HttpError as e:
            logger.error(f"[DRIVE] Error reading schedule: {e}")
            return None


# Singleton instance
drive_service = DriveService()
