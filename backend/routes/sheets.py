from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from services.sheets_service import sheets_service

router = APIRouter()


class CreateTabRequest(BaseModel):
    session_code: str


@router.get("")
async def list_sessions():
    """List all session tabs in the spreadsheet"""
    try:
        sessions = sheets_service.list_all_sessions()

        return {
            "sessions": sessions,
            "total": len(sessions),
            "spreadsheet_url": sheets_service.get_spreadsheet_url()
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_code}")
async def get_session(session_code: str):
    """Get session tab details by session code"""
    try:
        tab = sheets_service.find_session_tab(session_code)

        if not tab:
            raise HTTPException(status_code=404, detail=f"No tab found for session {session_code}")

        profiles = sheets_service.get_profiles(session_code)

        dates = set()
        for profile in profiles:
            for key in profile["attendance"].keys():
                if "Attendance" in key:
                    date = key.replace(" Attendance", "")
                    dates.add(date)

        return {
            "session_code": session_code,
            "name": tab["name"],
            "sheet_id": tab["sheet_id"],
            "spreadsheet_url": sheets_service.get_spreadsheet_url(),
            "profile_count": len(profiles),
            "dates": sorted(list(dates))
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_session_tab(request: CreateTabRequest):
    """Create a new session tab"""
    try:
        existing = sheets_service.find_session_tab(request.session_code)
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Tab for session {request.session_code} already exists"
            )

        tab = sheets_service.create_session_tab(request.session_code)

        return {
            "session_code": request.session_code,
            "name": tab["name"],
            "sheet_id": tab["sheet_id"],
            "spreadsheet_url": sheets_service.get_spreadsheet_url()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_code}/data")
async def get_session_data(session_code: str):
    """Get raw data from a session tab"""
    try:
        data = sheets_service.get_tab_data(session_code)

        if not data:
            return {"headers": [], "rows": []}

        return {
            "headers": data[0] if data else [],
            "rows": data[1:] if len(data) > 1 else [],
            "total_rows": len(data) - 1 if data else 0
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
