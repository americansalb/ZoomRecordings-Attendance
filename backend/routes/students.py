from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List

from services.sheets_service import sheets_service
from services.duplicate_detector import duplicate_detector

router = APIRouter()


class MergeProfilesRequest(BaseModel):
    spreadsheet_id: str
    keep_row: int
    merge_row: int


class UpdateProfileRequest(BaseModel):
    spreadsheet_id: str
    row_number: int
    first_name: str
    last_name: str
    email: str


@router.get("/search")
async def search_students(
    query: str = Query(..., min_length=1, description="Search query (name or email)"),
    session_code: Optional[str] = Query(None, description="Limit search to specific session")
):
    """
    Search for students across all sessions or within a specific session

    Used by students to find their profiles
    """
    try:
        results = []

        if session_code:
            # Search in specific session
            sheet = sheets_service.find_session_sheet(session_code)
            if sheet:
                profiles = sheets_service.get_profiles(sheet["id"])
                for profile in profiles:
                    if _matches_query(profile, query):
                        results.append({
                            "session_code": session_code,
                            "spreadsheet_id": sheet["id"],
                            "spreadsheet_name": sheet["name"],
                            **profile
                        })
        else:
            # Search across all sessions
            all_sheets = sheets_service.list_all_sheets()
            for sheet in all_sheets:
                # Extract session code from sheet name
                code = None
                if "Session " in sheet["name"]:
                    parts = sheet["name"].split("Session ")
                    if len(parts) > 1:
                        code = parts[1][:3] if len(parts[1]) >= 3 else parts[1].split()[0]

                try:
                    profiles = sheets_service.get_profiles(sheet["id"])
                    for profile in profiles:
                        if _matches_query(profile, query):
                            results.append({
                                "session_code": code,
                                "spreadsheet_id": sheet["id"],
                                "spreadsheet_name": sheet["name"],
                                **profile
                            })
                except Exception as e:
                    print(f"Error searching sheet {sheet['id']}: {e}")
                    continue

        return {
            "results": results,
            "total": len(results)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profile/{spreadsheet_id}/{row_number}")
async def get_student_profile(spreadsheet_id: str, row_number: int):
    """Get detailed profile for a specific student"""
    try:
        profiles = sheets_service.get_profiles(spreadsheet_id)

        for profile in profiles:
            if profile["row_number"] == row_number:
                # Calculate summary stats
                attendance_dates = []
                total_attendance = 0
                total_participation = 0

                for key, value in profile["attendance"].items():
                    if "Attendance" in key:
                        date = key.replace(" Attendance", "")
                        attendance_dates.append(date)
                        if isinstance(value, (int, float)):
                            total_attendance += value
                    elif "Participation" in key:
                        if isinstance(value, (int, float)):
                            total_participation += value

                return {
                    **profile,
                    "summary": {
                        "total_sessions": len(attendance_dates),
                        "total_attendance_minutes": total_attendance,
                        "total_participation_minutes": total_participation,
                        "average_attendance": total_attendance / len(attendance_dates) if attendance_dates else 0
                    }
                }

        raise HTTPException(status_code=404, detail="Profile not found")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/duplicates/{spreadsheet_id}")
async def find_duplicates(spreadsheet_id: str):
    """
    Find potential duplicate profiles in a session sheet

    Returns pairs of profiles that might be the same person
    """
    try:
        profiles = sheets_service.get_profiles(spreadsheet_id)
        duplicates = duplicate_detector.find_duplicates(profiles)

        return {
            "duplicates": [
                {
                    "profile1": {
                        "row": d.profile1_row,
                        "name": d.profile1_name
                    },
                    "profile2": {
                        "row": d.profile2_row,
                        "name": d.profile2_name
                    },
                    "confidence": d.confidence,
                    "reason": d.reason
                }
                for d in duplicates
            ],
            "total": len(duplicates)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/merge")
async def merge_profiles(request: MergeProfilesRequest):
    """
    Merge two profiles into one

    Keeps the first profile and merges attendance data from the second.
    The second profile is deleted after merging.
    """
    try:
        sheets_service.merge_profiles(
            request.spreadsheet_id,
            request.keep_row,
            request.merge_row
        )

        return {
            "success": True,
            "message": f"Profile at row {request.merge_row} merged into row {request.keep_row}"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/profile")
async def update_profile(request: UpdateProfileRequest):
    """Update a student's profile information"""
    try:
        sheets_service.update_profile(
            request.spreadsheet_id,
            request.row_number,
            request.first_name,
            request.last_name,
            request.email
        )

        return {"success": True}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{spreadsheet_id}")
async def get_session_students(spreadsheet_id: str):
    """Get all students in a session with their attendance data"""
    try:
        profiles = sheets_service.get_profiles(spreadsheet_id)

        # Extract unique dates from headers
        dates = set()
        for profile in profiles:
            for key in profile["attendance"].keys():
                if "Attendance" in key:
                    date = key.replace(" Attendance", "")
                    dates.add(date)

        return {
            "profiles": profiles,
            "total": len(profiles),
            "dates": sorted(list(dates))
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _matches_query(profile: dict, query: str) -> bool:
    """Check if a profile matches a search query"""
    query_lower = query.lower()

    # Check name
    full_name = f"{profile['first_name']} {profile['last_name']}".lower()
    if query_lower in full_name:
        return True

    # Check email
    if profile.get("email") and query_lower in profile["email"].lower():
        return True

    # Check first name alone
    if query_lower in profile["first_name"].lower():
        return True

    # Check last name alone
    if query_lower in profile["last_name"].lower():
        return True

    return False
