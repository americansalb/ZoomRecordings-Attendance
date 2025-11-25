from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, List

from services.sheets_service import sheets_service

router = APIRouter()


class NameMapping(BaseModel):
    zoom_name: str
    student_id: str
    first_name: str
    last_name: str
    session_code: Optional[str] = ""


class RosterStudent(BaseModel):
    student_id: str
    first_name: str
    last_name: str


@router.get("")
async def get_mappings(session_code: Optional[str] = None):
    """Get all name mappings, optionally filtered by session code"""
    try:
        mappings = sheets_service.get_name_mappings(session_code)
        return {
            "mappings": mappings,
            "total": len(mappings)
        }
    except Exception as e:
        print(f"[MAPPINGS] Error getting mappings: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("")
async def create_mapping(mapping: NameMapping):
    """Create a new name mapping"""
    try:
        result = sheets_service.add_name_mapping(
            zoom_name=mapping.zoom_name,
            student_id=mapping.student_id,
            first_name=mapping.first_name,
            last_name=mapping.last_name,
            session_code=mapping.session_code or ""
        )
        return {
            "success": True,
            "mapping": result
        }
    except Exception as e:
        print(f"[MAPPINGS] Error creating mapping: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{zoom_name:path}")
async def delete_mapping(zoom_name: str):
    """Delete a name mapping by Zoom name"""
    try:
        deleted = sheets_service.delete_name_mapping(zoom_name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Mapping not found for '{zoom_name}'")
        return {
            "success": True,
            "deleted": zoom_name
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[MAPPINGS] Error deleting mapping: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/roster/{session_code}")
async def get_roster(session_code: str):
    """Get the roster for a session (for UI to show student options)"""
    try:
        roster = sheets_service.get_roster(session_code)
        return {
            "roster": roster,
            "total": len(roster),
            "session_code": session_code
        }
    except Exception as e:
        print(f"[MAPPINGS] Error getting roster: {str(e)}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))
