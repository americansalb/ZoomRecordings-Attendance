from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from services.sheets_service import sheets_service
from services.zoom_service import zoom_service

router = APIRouter()


class CreateSheetRequest(BaseModel):
    session_code: str
    title: Optional[str] = None


@router.get("/")
async def list_sheets():
    """List all session sheets"""
    try:
        sheets = sheets_service.list_all_sheets()

        # Extract session codes from names
        processed = []
        for sheet in sheets:
            session_code = zoom_service.extract_session_code(sheet["name"])
            processed.append({
                "id": sheet["id"],
                "name": sheet["name"],
                "session_code": session_code,
                "url": f"https://docs.google.com/spreadsheets/d/{sheet['id']}"
            })

        return {
            "sheets": processed,
            "total": len(processed)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{session_code}")
async def get_sheet_by_session(session_code: str):
    """Get sheet details by session code"""
    try:
        sheet = sheets_service.find_session_sheet(session_code)

        if not sheet:
            raise HTTPException(status_code=404, detail=f"No sheet found for session {session_code}")

        profiles = sheets_service.get_profiles(sheet["id"])

        # Extract dates
        dates = set()
        for profile in profiles:
            for key in profile["attendance"].keys():
                if "Attendance" in key:
                    date = key.replace(" Attendance", "")
                    dates.add(date)

        return {
            "id": sheet["id"],
            "name": sheet["name"],
            "session_code": session_code,
            "url": f"https://docs.google.com/spreadsheets/d/{sheet['id']}",
            "profile_count": len(profiles),
            "dates": sorted(list(dates))
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/")
async def create_sheet(request: CreateSheetRequest):
    """Create a new session sheet"""
    try:
        # Check if sheet already exists
        existing = sheets_service.find_session_sheet(request.session_code)
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Sheet for session {request.session_code} already exists"
            )

        sheet = sheets_service.create_session_sheet(
            request.session_code,
            request.title
        )

        return {
            "id": sheet["id"],
            "name": sheet["name"],
            "session_code": request.session_code,
            "url": f"https://docs.google.com/spreadsheets/d/{sheet['id']}"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{spreadsheet_id}/data")
async def get_sheet_data(spreadsheet_id: str):
    """Get raw data from a sheet"""
    try:
        data = sheets_service.get_sheet_data(spreadsheet_id)

        if not data:
            return {"headers": [], "rows": []}

        return {
            "headers": data[0] if data else [],
            "rows": data[1:] if len(data) > 1 else [],
            "total_rows": len(data) - 1 if data else 0
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
