"""
Google Drive Service for Video Uploads

Handles uploading trimmed recordings to a shared Google Drive folder
with specific folder structure and permissions.
"""

import os
import logging
from typing import Optional, Dict, Any, List
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


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
    SCHEDULE_TAB_NAME = "All Official AALB Schedules"

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
            # Read the schedule spreadsheet
            result = self.sheets.spreadsheets().values().get(
                spreadsheetId=self.SCHEDULE_SPREADSHEET_ID,
                range=f"'{self.SCHEDULE_TAB_NAME}'!A:Z"
            ).execute()

            rows = result.get("values", [])
            if not rows:
                logger.warning("[DRIVE] Schedule spreadsheet is empty")
                return None

            headers = rows[0] if rows else []
            logger.info(f"[DRIVE] Schedule headers (first 10): {headers[:10]}")

            # Try multiple matching strategies

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
        Set file permissions: viewable with link, not downloadable.

        Note: Google Drive API doesn't have a direct "prevent download" option.
        The best we can do is set "reader" access which allows viewing.
        For true download prevention, you'd need to use Google Workspace settings.
        """
        try:
            # Create permission for anyone with the link to view
            permission = {
                'type': 'anyone',
                'role': 'reader'
            }

            self.drive.permissions().create(
                fileId=file_id,
                body=permission,
                supportsAllDrives=True
            ).execute()

            # Update file to restrict download/copy (Workspace feature)
            # This may not work for all accounts
            try:
                self.drive.files().update(
                    fileId=file_id,
                    body={
                        'copyRequiresWriterPermission': True,
                        'viewersCanCopyContent': False
                    },
                    supportsAllDrives=True
                ).execute()
            except HttpError:
                # This feature might not be available for all account types
                logger.warning("[DRIVE] Could not set download restriction (Workspace feature)")

            logger.info(f"[DRIVE] Set permissions for file {file_id}")
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
