from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime, timedelta
import logging

from services.zoom_service import zoom_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def list_recordings(
    from_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    search: Optional[str] = Query(None, description="Search term for recording title")
):
    """
    List all Zoom cloud recordings

    Returns recordings with session code extracted from title
    """
    try:
        # Default to last 30 days if no dates provided
        if not from_date:
            from_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")

        logger.info(f"GET /api/recordings - Fetching recordings from {from_date} to {to_date}")
        recordings = await zoom_service.list_all_recordings(from_date, to_date)
        logger.info(f"GET /api/recordings - Found {len(recordings)} recordings")

        # Process recordings to extract session codes and add metadata
        processed = []
        for recording in recordings:
            topic = recording.get("topic", "")
            session_code = zoom_service.extract_session_code(topic)

            rec_data = {
                "id": recording.get("uuid"),
                "meeting_id": recording.get("id"),
                "topic": topic,
                "session_code": session_code,
                "start_time": recording.get("start_time"),
                "duration": recording.get("duration"),
                "host_name": recording.get("host_name", ""),
                "host_email": recording.get("host_email", ""),
                "recording_count": recording.get("recording_count", 0),
                "total_size": recording.get("total_size", 0),
                "recording_files": [
                    {
                        "id": f.get("id"),
                        "file_type": f.get("file_type"),
                        "file_size": f.get("file_size"),
                        "download_url": f.get("download_url"),
                        "play_url": f.get("play_url"),
                        "recording_type": f.get("recording_type")
                    }
                    for f in recording.get("recording_files", [])
                ]
            }

            # Apply search filter if provided
            if search:
                if search.lower() not in topic.lower():
                    continue

            processed.append(rec_data)

        # Sort by start time (newest first)
        processed.sort(key=lambda x: x["start_time"] or "", reverse=True)

        return {
            "recordings": processed,
            "total": len(processed),
            "from_date": from_date,
            "to_date": to_date
        }

    except Exception as e:
        logger.error(f"GET /api/recordings - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{recording_id}")
async def get_recording(recording_id: str):
    """Get details about a specific recording"""
    try:
        # Get meeting details
        details = await zoom_service.get_past_meeting_details(recording_id)
        return details
    except Exception as e:
        logger.error(f"GET /api/recordings/{recording_id} - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{meeting_id}/participants")
async def get_recording_participants(meeting_id: str):
    """
    Get participant list for a recording/meeting

    Returns participants with calculated attendance duration
    """
    try:
        result = await zoom_service.get_meeting_participants(meeting_id)
        participants = result.get("participants", [])

        # Process participants to aggregate by unique user
        unique_participants = {}

        for p in participants:
            # Use email as primary key, fallback to name
            key = p.get("user_email") or p.get("name", "Unknown")

            if key not in unique_participants:
                # Parse name into first/last
                name = p.get("name", "")
                name_parts = name.split(" ", 1)
                first_name = name_parts[0] if name_parts else ""
                last_name = name_parts[1] if len(name_parts) > 1 else ""

                unique_participants[key] = {
                    "name": name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": p.get("user_email", ""),
                    "total_duration": 0,
                    "join_times": [],
                    "leave_times": []
                }

            # Add duration
            unique_participants[key]["total_duration"] += p.get("duration", 0)
            unique_participants[key]["join_times"].append(p.get("join_time"))
            unique_participants[key]["leave_times"].append(p.get("leave_time"))

        # Convert to list and calculate attendance minutes
        participant_list = []
        for key, data in unique_participants.items():
            # Duration from Zoom is in seconds, convert to minutes
            attendance_minutes = data["total_duration"] // 60

            participant_list.append({
                "name": data["name"],
                "first_name": data["first_name"],
                "last_name": data["last_name"],
                "email": data["email"],
                "attendance_minutes": attendance_minutes,
                "first_join": min(data["join_times"]) if data["join_times"] else None,
                "last_leave": max(data["leave_times"]) if data["leave_times"] else None
            })

        # Sort by name
        participant_list.sort(key=lambda x: x["name"].lower())

        return {
            "participants": participant_list,
            "total": len(participant_list)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
