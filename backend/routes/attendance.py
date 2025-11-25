from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timedelta

from services.zoom_service import zoom_service
from services.sheets_service import sheets_service

router = APIRouter()


class ProcessAttendanceRequest(BaseModel):
    meeting_id: str
    recording_title: str
    meeting_date: str  # Format: MM/DD
    meeting_duration_minutes: Optional[int] = None  # Scheduled meeting duration (if not provided, uses Zoom's actual duration)
    meeting_start_time: Optional[str] = None  # ISO format, if not provided will use Zoom's start time


class UpdateAttendanceRequest(BaseModel):
    session_code: str
    row_number: int
    date: str  # Format: MM/DD
    attendance_minutes: Optional[int] = None
    participation_minutes: Optional[int] = None


class BulkUpdateRequest(BaseModel):
    session_code: str
    date: str  # Format: MM/DD
    updates: List[dict]  # List of {row_number, attendance_minutes, participation_minutes}


@router.post("/process")
async def process_attendance(request: ProcessAttendanceRequest):
    """
    Process attendance from a Zoom meeting and update Google Sheet tab.

    Uses batch operations to minimize Google Sheets API calls and avoid rate limits.
    """
    try:
        print(f"[ATTENDANCE] Processing attendance for meeting {request.meeting_id}", flush=True)

        # Extract session code from title
        session_code = zoom_service.extract_session_code(request.recording_title)
        if not session_code:
            raise HTTPException(
                status_code=400,
                detail=f"Could not extract session code from title: {request.recording_title}"
            )

        print(f"[ATTENDANCE] Session code: {session_code}", flush=True)

        # Get or create session tab (1 API call)
        sheets_service.get_or_create_session_tab(session_code)

        # Get participants from Zoom
        participant_data = await zoom_service.get_meeting_participants(request.meeting_id)
        participants = participant_data.get("participants", [])

        print(f"[ATTENDANCE] Found {len(participants)} participant records from Zoom", flush=True)

        # Try to get meeting details from Zoom API
        zoom_start_time = None
        zoom_duration_minutes = None
        zoom_scheduled_start = None  # The SCHEDULED start time (what we really want)

        # First try past_meetings endpoint (actual meeting instance data)
        try:
            print(f"[ATTENDANCE] Fetching past_meetings for {request.meeting_id}...", flush=True)
            meeting_details = await zoom_service.get_past_meeting_details(request.meeting_id)
            zoom_start_time = meeting_details.get("start_time")  # ACTUAL start
            zoom_duration_minutes = meeting_details.get("duration")  # ACTUAL duration
            print(f"[ATTENDANCE] past_meetings: ACTUAL start={zoom_start_time}, ACTUAL duration={zoom_duration_minutes} min", flush=True)

            # Check if there's a scheduled_start_time or other fields we missed
            for key in ['scheduled_start_time', 'schedule_time', 'occurrence_start_time', 'settings']:
                if key in meeting_details:
                    print(f"[ATTENDANCE] past_meetings['{key}']: {meeting_details[key]}", flush=True)

            # Log ALL keys in the response
            print(f"[ATTENDANCE] past_meetings ALL KEYS: {list(meeting_details.keys())}", flush=True)
        except Exception as e:
            print(f"[ATTENDANCE] past_meetings FAILED: {e}", flush=True)

        # Try meetings endpoint using the NUMERIC meeting ID (not UUID) to get scheduled time
        zoom_scheduled_duration = None
        zoom_scheduled_time_pattern = None  # Just the time part (HH:MM) from occurrences
        try:
            # The past_meetings response has numeric 'id' which is the meeting series ID
            numeric_meeting_id = meeting_details.get("id") if meeting_details else None
            if numeric_meeting_id:
                print(f"[ATTENDANCE] Fetching meetings (schedule) using numeric ID {numeric_meeting_id}...", flush=True)
                schedule_details = await zoom_service.get_meeting_schedule(str(numeric_meeting_id))

                # For recurring meetings, start_time/duration are in occurrences, not top level
                occurrences = schedule_details.get("occurrences", [])
                if occurrences:
                    print(f"[ATTENDANCE] Found {len(occurrences)} occurrences:", flush=True)
                    for occ in occurrences[:3]:  # Log first 3
                        print(f"[ATTENDANCE]   occurrence: {occ}", flush=True)

                    # Extract the scheduled time pattern from the first occurrence
                    # All occurrences have the same time, just different dates
                    first_occ = occurrences[0]
                    zoom_scheduled_duration = first_occ.get("duration")  # e.g., 180
                    occ_start = first_occ.get("start_time")  # e.g., "2025-11-27T01:00:00Z"
                    if occ_start:
                        # Parse the time pattern (hour:minute in UTC)
                        occ_dt = datetime.fromisoformat(occ_start.replace("Z", "+00:00"))
                        zoom_scheduled_time_pattern = (occ_dt.hour, occ_dt.minute)
                        print(f"[ATTENDANCE] Extracted scheduled time pattern: {zoom_scheduled_time_pattern[0]:02d}:{zoom_scheduled_time_pattern[1]:02d} UTC, duration={zoom_scheduled_duration} min", flush=True)
                else:
                    # Non-recurring meeting - use top level start_time
                    zoom_scheduled_start = schedule_details.get("start_time")
                    zoom_scheduled_duration = schedule_details.get("duration")
                    print(f"[ATTENDANCE] meetings: SCHEDULED start={zoom_scheduled_start}, SCHEDULED duration={zoom_scheduled_duration} min", flush=True)

                # Log ALL keys
                print(f"[ATTENDANCE] meetings ALL KEYS: {list(schedule_details.keys())}", flush=True)
            else:
                print(f"[ATTENDANCE] No numeric meeting ID found in past_meetings response", flush=True)
        except Exception as e:
            print(f"[ATTENDANCE] meetings (schedule) FAILED: {e}", flush=True)

        # Determine meeting duration: user-provided > Zoom SCHEDULED (from occurrences) > Zoom ACTUAL > default
        if request.meeting_duration_minutes and request.meeting_duration_minutes > 0:
            meeting_duration = request.meeting_duration_minutes
            print(f"[ATTENDANCE] Duration: Using user-provided: {meeting_duration} min", flush=True)
        elif zoom_scheduled_duration and zoom_scheduled_duration > 0:
            meeting_duration = zoom_scheduled_duration
            print(f"[ATTENDANCE] Duration: Using Zoom SCHEDULED (from occurrences): {meeting_duration} min", flush=True)
        elif zoom_duration_minutes and zoom_duration_minutes > 0:
            meeting_duration = zoom_duration_minutes
            print(f"[ATTENDANCE] Duration: Using Zoom ACTUAL: {meeting_duration} min (WARNING: this is actual, not scheduled!)", flush=True)
        else:
            meeting_duration = 180  # Default 3 hours for typical sessions
            print(f"[ATTENDANCE] Duration: Using default: {meeting_duration} min", flush=True)

        # Determine scheduled window start time
        # Priority: user-provided > Zoom pattern (apply to actual date) > Zoom ACTUAL
        if request.meeting_start_time:
            scheduled_start = datetime.fromisoformat(request.meeting_start_time.replace("Z", "+00:00"))
            print(f"[ATTENDANCE] Start time: Using user-provided: {scheduled_start}", flush=True)
        elif zoom_scheduled_time_pattern and zoom_start_time:
            # Apply the scheduled time pattern to the actual meeting date
            actual_start = datetime.fromisoformat(zoom_start_time.replace("Z", "+00:00"))
            scheduled_start = actual_start.replace(
                hour=zoom_scheduled_time_pattern[0],
                minute=zoom_scheduled_time_pattern[1],
                second=0,
                microsecond=0
            )
            print(f"[ATTENDANCE] Start time: Using Zoom SCHEDULED pattern ({zoom_scheduled_time_pattern[0]:02d}:{zoom_scheduled_time_pattern[1]:02d} UTC) on actual date: {scheduled_start}", flush=True)
        elif zoom_scheduled_start:
            scheduled_start = datetime.fromisoformat(zoom_scheduled_start.replace("Z", "+00:00"))
            print(f"[ATTENDANCE] Start time: Using Zoom SCHEDULED: {scheduled_start}", flush=True)
        elif zoom_start_time:
            scheduled_start = datetime.fromisoformat(zoom_start_time.replace("Z", "+00:00"))
            print(f"[ATTENDANCE] Start time: Using Zoom ACTUAL: {scheduled_start} (WARNING: this is when host clicked start, not scheduled time!)", flush=True)
        else:
            scheduled_start = None
            print(f"[ATTENDANCE] Start time: NONE AVAILABLE - will fall back to raw durations", flush=True)

        if scheduled_start:
            scheduled_end = scheduled_start + timedelta(minutes=meeting_duration)
            print(f"[ATTENDANCE] Scheduled window: {scheduled_start} to {scheduled_end} ({meeting_duration} min)", flush=True)
        else:
            scheduled_end = None
            print(f"[ATTENDANCE] No scheduled window - will cap at {meeting_duration} min", flush=True)

        # Aggregate participants by unique user, calculating ONLY time within scheduled window
        unique_participants = {}
        for p in participants:
            key = p.get("user_email") or p.get("name", "Unknown")

            if key not in unique_participants:
                name = p.get("name", "")
                name_parts = name.split(" ", 1)
                unique_participants[key] = {
                    "first_name": name_parts[0] if name_parts else "",
                    "last_name": name_parts[1] if len(name_parts) > 1 else "",
                    "email": p.get("user_email", ""),
                    "total_duration": 0
                }

            # Calculate time within scheduled window for this join/leave session
            join_time = p.get("join_time")
            leave_time = p.get("leave_time")

            if join_time and leave_time and scheduled_start and scheduled_end:
                # Use the helper function to calculate overlap with scheduled window
                session_minutes = zoom_service.calculate_attendance_minutes(
                    join_time, leave_time, scheduled_start, scheduled_end
                )
                unique_participants[key]["total_duration"] += session_minutes * 60  # Convert to seconds
            else:
                # Fallback: use Zoom's reported duration
                unique_participants[key]["total_duration"] += p.get("duration", 0)

        print(f"[ATTENDANCE] Aggregated to {len(unique_participants)} unique participants", flush=True)

        # Final cap to meeting duration (safety check)
        max_duration_seconds = meeting_duration * 60
        for key in unique_participants:
            if unique_participants[key]["total_duration"] > max_duration_seconds:
                print(f"[ATTENDANCE] Final cap {key}: {unique_participants[key]['total_duration']}s -> {max_duration_seconds}s", flush=True)
                unique_participants[key]["total_duration"] = max_duration_seconds

        # Use the new batch processing method (minimizes API calls)
        participant_list = list(unique_participants.values())
        results = sheets_service.process_attendance_batch(
            session_code,
            request.meeting_date,
            participant_list
        )

        # Auto-generate the Summary tab with aggregated data
        print(f"[ATTENDANCE] Generating summary tab for session {session_code}", flush=True)
        summary_result = sheets_service.generate_session_summary(session_code)
        print(f"[ATTENDANCE] Summary tab updated with {summary_result['students']} students", flush=True)

        return {
            "success": True,
            "session_code": session_code,
            "meeting_date": request.meeting_date,
            "spreadsheet_url": sheets_service.get_spreadsheet_url(),
            "results": results,
            "summary": summary_result
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ATTENDANCE] Error: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update")
async def update_attendance(request: UpdateAttendanceRequest):
    """Update attendance or participation for a specific profile and date"""
    try:
        columns = sheets_service.get_or_add_date_columns(request.session_code, request.date)

        if request.attendance_minutes is not None:
            sheets_service.update_attendance(
                request.session_code,
                request.row_number,
                columns["attendance_col"],
                request.attendance_minutes
            )

        if request.participation_minutes is not None:
            sheets_service.update_participation(
                request.session_code,
                request.row_number,
                columns["participation_col"],
                request.participation_minutes
            )

        return {"success": True}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/bulk-update")
async def bulk_update_attendance(request: BulkUpdateRequest):
    """Bulk update attendance and participation for multiple profiles"""
    try:
        columns = sheets_service.get_or_add_date_columns(request.session_code, request.date)

        attendance_updates = []
        participation_updates = []

        for update in request.updates:
            if "attendance_minutes" in update and update["attendance_minutes"] is not None:
                attendance_updates.append({
                    "row": update["row_number"],
                    "col": columns["attendance_col"],
                    "value": update["attendance_minutes"]
                })

            if "participation_minutes" in update and update["participation_minutes"] is not None:
                participation_updates.append({
                    "row": update["row_number"],
                    "col": columns["participation_col"],
                    "value": update["participation_minutes"]
                })

        if attendance_updates:
            sheets_service.batch_update_attendance(request.session_code, attendance_updates)

        if participation_updates:
            sheets_service.batch_update_attendance(request.session_code, participation_updates)

        return {
            "success": True,
            "updated": len(attendance_updates) + len(participation_updates)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/preview/{meeting_id}")
async def preview_attendance(meeting_id: str, recording_title: str):
    """Preview attendance data before processing"""
    try:
        session_code = zoom_service.extract_session_code(recording_title)

        participant_data = await zoom_service.get_meeting_participants(meeting_id)
        participants = participant_data.get("participants", [])

        existing_tab = None
        if session_code:
            existing_tab = sheets_service.find_session_tab(session_code)

        unique_participants = {}
        for p in participants:
            key = p.get("user_email") or p.get("name", "Unknown")

            if key not in unique_participants:
                name = p.get("name", "")
                name_parts = name.split(" ", 1)
                unique_participants[key] = {
                    "first_name": name_parts[0] if name_parts else "",
                    "last_name": name_parts[1] if len(name_parts) > 1 else "",
                    "email": p.get("user_email", ""),
                    "total_duration": 0
                }

            unique_participants[key]["total_duration"] += p.get("duration", 0)

        preview = []
        existing_profiles = []

        if existing_tab:
            existing_profiles = sheets_service.get_profiles(session_code)

        for key, data in unique_participants.items():
            is_new = True
            matched_row = None

            for profile in existing_profiles:
                if data["email"] and profile["email"].lower() == data["email"].lower():
                    is_new = False
                    matched_row = profile["row_number"]
                    break
                if (profile["first_name"].lower() == data["first_name"].lower() and
                        profile["last_name"].lower() == data["last_name"].lower()):
                    is_new = False
                    matched_row = profile["row_number"]
                    break

            preview.append({
                "name": f"{data['first_name']} {data['last_name']}",
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "email": data["email"],
                "attendance_minutes": data["total_duration"] // 60,
                "is_new": is_new,
                "matched_row": matched_row
            })

        preview.sort(key=lambda x: x["name"].lower())

        return {
            "session_code": session_code,
            "existing_tab": existing_tab,
            "participants": preview,
            "new_count": sum(1 for p in preview if p["is_new"]),
            "existing_count": sum(1 for p in preview if not p["is_new"])
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
