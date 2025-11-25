from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional

from services.sheets_service import sheets_service
from services.duplicate_detector import duplicate_detector

router = APIRouter()


class MergeProfilesRequest(BaseModel):
    session_code: str
    keep_row: int
    merge_row: int


class UpdateProfileRequest(BaseModel):
    session_code: str
    row_number: int
    first_name: str
    last_name: str
    email: str


@router.get("/search")
async def search_students(
    query: str = Query(..., min_length=1, description="Search query (name or email)"),
    session_code: Optional[str] = Query(None, description="Limit search to specific session")
):
    """Search for students across all sessions or within a specific session"""
    try:
        results = []

        if session_code:
            # Search in specific session
            tab = sheets_service.find_session_tab(session_code)
            if tab:
                profiles = sheets_service.get_profiles(session_code)
                for profile in profiles:
                    if _matches_query(profile, query):
                        results.append({
                            "session_code": session_code,
                            "session_name": tab["name"],
                            **profile
                        })
        else:
            # Search across all sessions
            all_sessions = sheets_service.list_all_sessions()
            for session in all_sessions:
                try:
                    profiles = sheets_service.get_profiles(session["session_code"])
                    for profile in profiles:
                        if _matches_query(profile, query):
                            results.append({
                                "session_code": session["session_code"],
                                "session_name": session["name"],
                                **profile
                            })
                except Exception as e:
                    print(f"Error searching session {session['session_code']}: {e}")
                    continue

        return {
            "results": results,
            "total": len(results)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/profile/{session_code}/{row_number}")
async def get_student_profile(session_code: str, row_number: int):
    """Get detailed profile for a specific student"""
    try:
        profiles = sheets_service.get_profiles(session_code)

        for profile in profiles:
            if profile["row_number"] == row_number:
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
                    "session_code": session_code,
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


@router.get("/duplicates/{session_code}")
async def find_duplicates(session_code: str):
    """Find potential duplicate profiles in a session"""
    try:
        profiles = sheets_service.get_profiles(session_code)
        duplicates = duplicate_detector.find_duplicates(profiles)

        return {
            "duplicates": [
                {
                    "profile1": {"row": d.profile1_row, "name": d.profile1_name},
                    "profile2": {"row": d.profile2_row, "name": d.profile2_name},
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
    """Merge two profiles into one"""
    try:
        sheets_service.merge_profiles(
            request.session_code,
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
            request.session_code,
            request.row_number,
            request.first_name,
            request.last_name,
            request.email
        )

        return {"success": True}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{session_code}")
async def get_session_students(session_code: str):
    """Get all students in a session with their attendance data"""
    try:
        profiles = sheets_service.get_profiles(session_code)

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

    full_name = f"{profile['first_name']} {profile['last_name']}".lower()
    if query_lower in full_name:
        return True

    if profile.get("email") and query_lower in profile["email"].lower():
        return True

    if query_lower in profile["first_name"].lower():
        return True

    if query_lower in profile["last_name"].lower():
        return True

    return False


def _matches_summary_query(student: dict, query: str) -> bool:
    """Check if a summary student matches a search query"""
    query_lower = query.lower()

    # Check canonical name
    full_name = f"{student['first_name']} {student['last_name']}".lower()
    if query_lower in full_name:
        return True

    # Check student ID
    if student.get("student_id") and query_lower in student["student_id"].lower():
        return True

    # Check known Zoom names
    for zoom_name in student.get("known_zoom_names", []):
        if query_lower in zoom_name.lower():
            return True

    return False


# ==================== SUMMARY-BASED STUDENT ENDPOINTS ====================

@router.get("/summary/search")
async def search_students_summary(
    query: str = Query(..., min_length=1, description="Search query (name, student ID, or Zoom name)"),
    session_code: Optional[str] = Query(None, description="Limit search to specific session")
):
    """
    Search for students using summary data (canonical roster names).

    This searches across:
    - Canonical roster names
    - Student IDs
    - All known Zoom name variations
    """
    try:
        results = []

        if session_code:
            # Search in specific session summary
            try:
                summary_data = sheets_service.get_summary_data(session_code)
                for student in summary_data.get("students", []):
                    if _matches_summary_query(student, query):
                        results.append({
                            "session_code": session_code,
                            "session_name": f"Session {session_code}",
                            **student
                        })
            except Exception as e:
                print(f"Error searching summary for session {session_code}: {e}")
        else:
            # Search across all sessions
            all_sessions = sheets_service.list_all_sessions()
            for session in all_sessions:
                try:
                    summary_data = sheets_service.get_summary_data(session["session_code"])
                    for student in summary_data.get("students", []):
                        if _matches_summary_query(student, query):
                            results.append({
                                "session_code": session["session_code"],
                                "session_name": session["name"],
                                **student
                            })
                except Exception as e:
                    print(f"Error searching summary for session {session['session_code']}: {e}")
                    continue

        return {
            "results": results,
            "total": len(results)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/summary/profile/{session_code}/{row_number}")
async def get_student_summary_profile(session_code: str, row_number: int):
    """
    Get detailed summary profile for a specific student.

    Shows:
    - Canonical roster name
    - All known Zoom name variations
    - Attendance per date
    """
    try:
        summary_data = sheets_service.get_summary_data(session_code)

        for student in summary_data.get("students", []):
            if student["row_number"] == row_number:
                # Calculate summary stats
                attendance_values = [v for v in student.get("attendance", {}).values() if isinstance(v, (int, float))]
                total_attendance = sum(attendance_values)
                sessions_attended = sum(1 for v in attendance_values if v > 0)

                return {
                    **student,
                    "session_code": session_code,
                    "dates": summary_data.get("dates", []),
                    "summary": {
                        "total_sessions": sessions_attended,
                        "total_attendance_minutes": total_attendance,
                        "average_attendance": total_attendance / sessions_attended if sessions_attended > 0 else 0
                    }
                }

        raise HTTPException(status_code=404, detail="Profile not found in summary")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
