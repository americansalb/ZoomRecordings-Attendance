"""
Video Proctoring API Routes

Endpoints for processing Zoom gallery view recordings and generating
video participation reports.
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from typing import List, Optional, Dict
import os
import tempfile
import requests
import logging
from datetime import datetime

from services.proctoring import VideoProctorService
from services.sheets_service import sheets_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/proctor", tags=["proctoring"])

# Background job tracking
processing_jobs: Dict[str, Dict] = {}


class ProctorRequest(BaseModel):
    """Request to process a recording for video participation."""
    meeting_id: str
    recording_title: str
    session_code: str
    meeting_date: str
    video_url: str  # URL to the gallery view recording
    participant_names: List[str]  # Names of participants in grid order
    grid_layout: Optional[List[int]] = None  # [rows, cols], auto-detect if None
    sample_interval: Optional[float] = 30.0  # Seconds between samples


class ProctorJobStatus(BaseModel):
    """Status of a proctoring job."""
    job_id: str
    status: str  # "pending", "processing", "completed", "failed"
    progress: float  # 0.0 to 1.0
    message: str
    result: Optional[Dict] = None
    error: Optional[str] = None


class WarningDocumentRequest(BaseModel):
    """Request to generate a warning document."""
    job_id: str
    participant_name: str
    min_violation_minutes: Optional[float] = 1.0


@router.post("/process")
async def start_proctoring(
    request: ProctorRequest,
    background_tasks: BackgroundTasks
) -> Dict:
    """
    Start processing a recording for video participation analysis.

    This runs in the background and returns a job ID to track progress.
    """
    # Generate job ID
    job_id = f"proctor_{request.meeting_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    # Initialize job status
    processing_jobs[job_id] = {
        "status": "pending",
        "progress": 0.0,
        "message": "Job queued",
        "request": request.dict(),
        "result": None,
        "error": None
    }

    # Start background processing
    background_tasks.add_task(
        process_recording_background,
        job_id,
        request
    )

    return {
        "success": True,
        "job_id": job_id,
        "message": "Proctoring job started"
    }


async def process_recording_background(job_id: str, request: ProctorRequest):
    """Background task to process a recording."""
    try:
        processing_jobs[job_id]["status"] = "processing"
        processing_jobs[job_id]["message"] = "Downloading video..."
        processing_jobs[job_id]["progress"] = 0.1

        # Create temp directory for this job
        temp_dir = tempfile.mkdtemp(prefix=f"proctor_{job_id}_")
        video_path = os.path.join(temp_dir, "recording.mp4")

        # Download video
        logger.info(f"[PROCTOR] Downloading video for job {job_id}")
        response = requests.get(request.video_url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0

        with open(video_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    download_progress = downloaded / total_size
                    processing_jobs[job_id]["progress"] = 0.1 + (download_progress * 0.2)

        processing_jobs[job_id]["message"] = "Analyzing video..."
        processing_jobs[job_id]["progress"] = 0.3

        # Initialize video processor
        proctor_service = VideoProctorService(
            output_dir=temp_dir,
            sample_interval=request.sample_interval
        )

        # Process video
        grid_layout = tuple(request.grid_layout) if request.grid_layout else None
        report = proctor_service.process_video(
            video_path=video_path,
            participant_names=request.participant_names,
            recording_id=request.meeting_id,
            recording_title=request.recording_title,
            session_code=request.session_code,
            meeting_date=request.meeting_date,
            grid_layout=grid_layout
        )

        processing_jobs[job_id]["progress"] = 0.8
        processing_jobs[job_id]["message"] = "Saving results..."

        # Save report
        report_path = proctor_service.save_report_json(report)

        # Update Google Sheet with video participation data
        try:
            update_video_participation_sheet(
                session_code=request.session_code,
                meeting_date=request.meeting_date,
                report=report
            )
            processing_jobs[job_id]["message"] = "Results saved to Google Sheets"
        except Exception as e:
            logger.error(f"[PROCTOR] Failed to update sheets: {e}")
            processing_jobs[job_id]["message"] = f"Results saved (sheet update failed: {e})"

        # Build result summary
        result = {
            "recording_id": report.recording_id,
            "session_code": report.session_code,
            "meeting_date": report.meeting_date,
            "total_duration_minutes": report.total_duration_seconds / 60,
            "frames_analyzed": report.frames_analyzed,
            "report_path": report_path,
            "screenshots_dir": report.screenshots_dir,
            "participants": []
        }

        for p in report.participants:
            result["participants"].append({
                "name": p.name,
                "visibility_percentage": p.visibility_percentage,
                "violation_count": len(p.violations),
                "total_violation_minutes": sum(v.duration for v in p.violations) / 60,
                "issues": p.issues_summary
            })

        processing_jobs[job_id]["status"] = "completed"
        processing_jobs[job_id]["progress"] = 1.0
        processing_jobs[job_id]["result"] = result

        # Cleanup video processor
        proctor_service.cleanup()

        logger.info(f"[PROCTOR] Job {job_id} completed successfully")

    except Exception as e:
        logger.error(f"[PROCTOR] Job {job_id} failed: {e}", exc_info=True)
        processing_jobs[job_id]["status"] = "failed"
        processing_jobs[job_id]["error"] = str(e)
        processing_jobs[job_id]["message"] = f"Processing failed: {e}"


def update_video_participation_sheet(
    session_code: str,
    meeting_date: str,
    report
):
    """
    Update Google Sheet with video participation data.

    Creates/updates a "Video Participation" tab with visibility percentages.
    """
    tab_name = f"Video Participation {session_code}"
    logger.info(f"[PROCTOR] Updating sheet '{tab_name}' for {meeting_date}")

    try:
        # Get or create Video Participation tab
        video_tab = sheets_service.get_or_create_video_participation_tab(session_code)

        if not video_tab:
            logger.error(f"[PROCTOR] Failed to get/create video participation tab")
            return

        # Read existing data to check for date column
        existing_data = sheets_service.get_video_participation_data(session_code)
        headers = existing_data[0] if existing_data else ["Student Name"]
        rows = existing_data[1:] if len(existing_data) > 1 else []

        # Check if date column already exists
        date_header = f"{meeting_date} Visibility %"
        violation_header = f"{meeting_date} Violations"

        date_col_idx = None
        violation_col_idx = None

        for idx, header in enumerate(headers):
            if header == date_header:
                date_col_idx = idx
            elif header == violation_header:
                violation_col_idx = idx

        # Add new columns if needed
        if date_col_idx is None:
            headers.append(date_header)
            date_col_idx = len(headers) - 1
            headers.append(violation_header)
            violation_col_idx = len(headers) - 1

        # Build name to row mapping
        name_to_row = {}
        for idx, row in enumerate(rows):
            if row and len(row) > 0:
                name_to_row[row[0].lower().strip()] = idx

        # Update or add rows for each participant
        for p in report.participants:
            name_key = p.name.lower().strip()

            if name_key in name_to_row:
                # Update existing row
                row_idx = name_to_row[name_key]
                row = rows[row_idx]

                # Extend row if needed
                while len(row) < len(headers):
                    row.append("")

                row[date_col_idx] = f"{p.visibility_percentage:.1f}"
                row[violation_col_idx] = str(len(p.violations))
            else:
                # Add new row
                new_row = [p.name] + [""] * (len(headers) - 1)
                new_row[date_col_idx] = f"{p.visibility_percentage:.1f}"
                new_row[violation_col_idx] = str(len(p.violations))
                rows.append(new_row)
                name_to_row[name_key] = len(rows) - 1

        # Write back to sheet
        all_data = [headers] + rows
        sheets_service.write_video_participation_data(session_code, all_data)

        logger.info(f"[PROCTOR] Updated {len(report.participants)} participants in sheet")

    except Exception as e:
        logger.error(f"[PROCTOR] Error updating sheet: {e}", exc_info=True)


@router.get("/status/{job_id}")
async def get_job_status(job_id: str) -> ProctorJobStatus:
    """Get the status of a proctoring job."""
    if job_id not in processing_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job = processing_jobs[job_id]
    return ProctorJobStatus(
        job_id=job_id,
        status=job["status"],
        progress=job["progress"],
        message=job["message"],
        result=job.get("result"),
        error=job.get("error")
    )


@router.get("/results/{job_id}")
async def get_job_results(job_id: str) -> Dict:
    """Get the full results of a completed proctoring job."""
    if job_id not in processing_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    job = processing_jobs[job_id]

    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not complete. Status: {job['status']}"
        )

    return {
        "success": True,
        "job_id": job_id,
        "result": job["result"]
    }


@router.post("/warning")
async def generate_warning_document(request: WarningDocumentRequest) -> Dict:
    """
    Generate a warning document for a participant.

    Returns the warning text and screenshots as base64.
    """
    if request.job_id not in processing_jobs:
        raise HTTPException(status_code=404, detail=f"Job {request.job_id} not found")

    job = processing_jobs[request.job_id]

    if job["status"] != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not complete. Status: {job['status']}"
        )

    # Load the full report
    report_path = job["result"].get("report_path")
    if not report_path or not os.path.exists(report_path):
        raise HTTPException(status_code=404, detail="Report file not found")

    import json
    with open(report_path) as f:
        report_data = json.load(f)

    # Find participant
    participant = None
    for p in report_data["participants"]:
        if p["name"] == request.participant_name:
            participant = p
            break

    if not participant:
        raise HTTPException(
            status_code=404,
            detail=f"Participant '{request.participant_name}' not found"
        )

    # Filter significant violations
    min_duration = request.min_violation_minutes * 60
    significant_violations = [
        v for v in participant["violations"]
        if v["duration"] >= min_duration
    ]

    if not significant_violations:
        return {
            "success": True,
            "has_violations": False,
            "participant_name": request.participant_name,
            "message": "No significant video visibility issues detected."
        }

    # Build warning document
    total_violation_time = sum(v["duration"] for v in significant_violations)

    document = {
        "success": True,
        "has_violations": True,
        "participant_name": request.participant_name,
        "session_code": report_data["session_code"],
        "meeting_date": report_data["meeting_date"],
        "meeting_duration_minutes": report_data["total_duration_seconds"] / 60,
        "visibility_percentage": participant["visibility_percentage"],
        "total_violation_minutes": total_violation_time / 60,
        "violation_count": len(significant_violations),
        "violations": [],
        "screenshots": []
    }

    # Format violations
    import base64
    for v in significant_violations:
        hours = int(v["start_time"] // 3600)
        mins = int((v["start_time"] % 3600) // 60)
        secs = int(v["start_time"] % 60)
        start_str = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"

        hours = int(v["end_time"] // 3600)
        mins = int((v["end_time"] % 3600) // 60)
        secs = int(v["end_time"] % 60)
        end_str = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"

        document["violations"].append({
            "type": v["violation_type"],
            "start_time": start_str,
            "end_time": end_str,
            "duration_minutes": v["duration"] / 60
        })

        # Include screenshot if available
        if v.get("screenshot_path") and os.path.exists(v["screenshot_path"]):
            with open(v["screenshot_path"], "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
                document["screenshots"].append({
                    "timestamp": start_str,
                    "data": screenshot_b64,
                    "filename": os.path.basename(v["screenshot_path"])
                })

    # Generate warning text
    document["warning_text"] = f"""
VIDEO PARTICIPATION WARNING

Student: {document['participant_name']}
Session: {document['session_code']}
Date: {document['meeting_date']}

Meeting Duration: {document['meeting_duration_minutes']:.0f} minutes
Your Video Visibility: {document['visibility_percentage']:.1f}%
Total Time Without Video: {document['total_violation_minutes']:.1f} minutes

VIOLATIONS DETECTED:
"""
    for i, v in enumerate(document["violations"], 1):
        document["warning_text"] += f"""
{i}. {v['type'].replace('_', ' ').title()}
   Time: {v['start_time']} - {v['end_time']} ({v['duration_minutes']:.1f} min)
"""

    document["warning_text"] += """
REMINDER:
Students are required to have their camera on and face visible throughout
the session. Please ensure your camera is working properly and you remain
visible on screen for future sessions.

If you believe this is an error, please contact your instructor.
"""

    return document


@router.get("/jobs")
async def list_jobs() -> Dict:
    """List all proctoring jobs."""
    jobs = []
    for job_id, job in processing_jobs.items():
        jobs.append({
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "session_code": job["request"].get("session_code"),
            "meeting_date": job["request"].get("meeting_date")
        })

    return {
        "jobs": jobs,
        "total": len(jobs)
    }
