from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

from services.zoom_service import zoom_service
from services.sheets_service import sheets_service

router = APIRouter()


class ProcessAttendanceRequest(BaseModel):
    meeting_id: str
    recording_title: str
    meeting_date: str  # Format: MM/DD
    spreadsheet_id: Optional[str] = None  # If None, will find/create based on session code


class UpdateAttendanceRequest(BaseModel):
    spreadsheet_id: str
    row_number: int
    date: str  # Format: MM/DD
    attendance_minutes: Optional[int] = None
    participation_minutes: Optional[int] = None


class BulkUpdateRequest(BaseModel):
    spreadsheet_id: str
    date: str  # Format: MM/DD
    updates: List[dict]  # List of {row_number, attendance_minutes, participation_minutes}


@router.post("/process")
async def process_attendance(request: ProcessAttendanceRequest):
    """
    Process attendance from a Zoom meeting and update Google Sheet

    1. Gets participant data from Zoom
    2. Finds or creates the session sheet based on recording title
    3. Adds/updates profiles for each participant
    4. Records attendance minutes for the meeting date
    """
    try:
        # Extract session code from title
        session_code = zoom_service.extract_session_code(request.recording_title)
        if not session_code:
            raise HTTPException(
                status_code=400,
                detail=f"Could not extract session code from title: {request.recording_title}"
            )

        # Get or use provided spreadsheet
        if request.spreadsheet_id:
            sheet = {"id": request.spreadsheet_id}
        else:
            sheet = sheets_service.get_or_create_session_sheet(
                session_code,
                title=f"Session {session_code}"
            )

        spreadsheet_id = sheet["id"]

        # Get participants from Zoom
        participant_data = await zoom_service.get_meeting_participants(request.meeting_id)
        participants = participant_data.get("participants", [])

        # Aggregate participants by unique user
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

        # Ensure date columns exist
        columns = sheets_service.get_or_add_date_columns(spreadsheet_id, request.meeting_date)

        # Process each participant
        results = {
            "new_profiles": 0,
            "updated_profiles": 0,
            "profiles": []
        }

        batch_updates = []

        for key, data in unique_participants.items():
            # Find existing profile or create new one
            row = sheets_service.find_profile_row(
                spreadsheet_id,
                data["first_name"],
                data["last_name"],
                data["email"]
            )

            if row is None:
                # Add new profile
                row = sheets_service.add_profile(
                    spreadsheet_id,
                    data["first_name"],
                    data["last_name"],
                    data["email"]
                )
                results["new_profiles"] += 1
            else:
                results["updated_profiles"] += 1

            # Calculate attendance minutes (Zoom returns duration in seconds)
            attendance_minutes = data["total_duration"] // 60

            # Queue attendance update
            batch_updates.append({
                "row": row,
                "col": columns["attendance_col"],
                "value": attendance_minutes
            })

            results["profiles"].append({
                "row": row,
                "name": f"{data['first_name']} {data['last_name']}",
                "email": data["email"],
                "attendance_minutes": attendance_minutes
            })

        # Batch update attendance
        if batch_updates:
            sheets_service.batch_update_attendance(spreadsheet_id, batch_updates)

        return {
            "success": True,
            "spreadsheet_id": spreadsheet_id,
            "session_code": session_code,
            "meeting_date": request.meeting_date,
            "results": results
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update")
async def update_attendance(request: UpdateAttendanceRequest):
    """Update attendance or participation for a specific profile and date"""
    try:
        columns = sheets_service.get_or_add_date_columns(
            request.spreadsheet_id,
            request.date
        )

        if request.attendance_minutes is not None:
            sheets_service.update_attendance(
                request.spreadsheet_id,
                request.row_number,
                columns["attendance_col"],
                request.attendance_minutes
            )

        if request.participation_minutes is not None:
            sheets_service.update_participation(
                request.spreadsheet_id,
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
        columns = sheets_service.get_or_add_date_columns(
            request.spreadsheet_id,
            request.date
        )

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
            sheets_service.batch_update_attendance(request.spreadsheet_id, attendance_updates)

        if participation_updates:
            sheets_service.batch_update_attendance(request.spreadsheet_id, participation_updates)

        return {
            "success": True,
            "updated": len(attendance_updates) + len(participation_updates)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/preview/{meeting_id}")
async def preview_attendance(meeting_id: str, recording_title: str):
    """
    Preview attendance data before processing

    Shows what will be added/updated without making changes
    """
    try:
        # Extract session code
        session_code = zoom_service.extract_session_code(recording_title)

        # Get participants from Zoom
        participant_data = await zoom_service.get_meeting_participants(meeting_id)
        participants = participant_data.get("participants", [])

        # Find existing sheet if any
        existing_sheet = None
        if session_code:
            existing_sheet = sheets_service.find_session_sheet(session_code)

        # Aggregate participants
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

        # Check against existing profiles if sheet exists
        preview = []
        existing_profiles = []

        if existing_sheet:
            existing_profiles = sheets_service.get_profiles(existing_sheet["id"])

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
            "existing_sheet": existing_sheet,
            "participants": preview,
            "new_count": sum(1 for p in preview if p["is_new"]),
            "existing_count": sum(1 for p in preview if not p["is_new"])
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
