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
    meeting_duration_minutes: int = 60  # Scheduled meeting duration
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

        # Try to get scheduled meeting details from Zoom API (requires meeting:read:past_meeting:admin scope)
        zoom_start_time = None
        zoom_duration_minutes = None
        try:
            meeting_details = await zoom_service.get_past_meeting_details(request.meeting_id)
            zoom_start_time = meeting_details.get("start_time")
            zoom_duration_minutes = meeting_details.get("duration")
            print(f"[ATTENDANCE] Zoom API: start={zoom_start_time}, duration={zoom_duration_minutes} min", flush=True)
        except Exception as e:
            print(f"[ATTENDANCE] Could not get meeting details (add meeting:read:past_meeting:admin scope): {e}", flush=True)

        # Determine meeting duration: Zoom API > user-provided (NO participant fallback - that defeats the purpose)
        if zoom_duration_minutes and zoom_duration_minutes > 0:
            meeting_duration = zoom_duration_minutes
            print(f"[ATTENDANCE] Using Zoom API duration: {meeting_duration} min", flush=True)
        else:
            meeting_duration = request.meeting_duration_minutes
            print(f"[ATTENDANCE] Using user-provided duration: {meeting_duration} min", flush=True)

        # Determine scheduled window start time: Zoom API > user-provided (NO earliest_join fallback)
        if request.meeting_start_time:
            scheduled_start = datetime.fromisoformat(request.meeting_start_time.replace("Z", "+00:00"))
            print(f"[ATTENDANCE] Using user-provided start time: {scheduled_start}", flush=True)
        elif zoom_start_time:
            scheduled_start = datetime.fromisoformat(zoom_start_time.replace("Z", "+00:00"))
            print(f"[ATTENDANCE] Using Zoom API start time: {scheduled_start}", flush=True)
        else:
            scheduled_start = None
            print(f"[ATTENDANCE] WARNING: No scheduled start time available", flush=True)

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
