"""
Video Proctor Service

Processes Zoom gallery view recordings to analyze participant video presence.
Generates reports with screenshots and visibility timelines.
"""

import cv2
import os
import json
import tempfile
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from pathlib import Path
import logging
from dataclasses import dataclass, asdict
from collections import defaultdict
import base64

from .face_detector import FaceDetector

logger = logging.getLogger(__name__)


@dataclass
class ParticipantViolation:
    """Record of a video visibility violation."""
    participant_name: str
    violation_type: str  # "no_face", "multiple_faces", "face_too_small"
    start_time: float  # seconds from video start
    end_time: float
    duration: float  # seconds
    screenshot_path: Optional[str] = None
    screenshot_base64: Optional[str] = None


@dataclass
class ParticipantReport:
    """Summary report for a single participant."""
    name: str
    grid_position: Tuple[int, int]
    total_frames: int
    visible_frames: int
    visibility_percentage: float
    violations: List[ParticipantViolation]
    issues_summary: Dict[str, int]  # {"no_face": 5, "multiple_faces": 1}


@dataclass
class ProctorReport:
    """Full proctoring report for a recording."""
    recording_id: str
    recording_title: str
    session_code: str
    meeting_date: str
    processing_date: str
    total_duration_seconds: float
    frames_analyzed: int
    sample_interval_seconds: float
    participants: List[ParticipantReport]
    screenshots_dir: Optional[str] = None


class VideoProctorService:
    """
    Service for processing Zoom gallery view recordings.

    Analyzes participant video presence and generates reports.
    """

    def __init__(
        self,
        output_dir: str = "/tmp/proctor_output",
        sample_interval: float = 30.0,  # Sample every 30 seconds
        face_detection_backend: str = "opencv"
    ):
        """
        Initialize the video proctor service.

        Args:
            output_dir: Directory to save screenshots and reports
            sample_interval: Seconds between frame samples
            face_detection_backend: Face detection backend to use
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.sample_interval = sample_interval
        self.face_detector = FaceDetector(backend=face_detection_backend)

        # Grid layout detection settings
        self.min_grid_size = 1
        self.max_grid_size = 7  # Max 7x7 = 49 participants

        logger.info(f"VideoProctorService initialized (output: {self.output_dir})")

    def process_video(
        self,
        video_path: str,
        participant_names: List[str],
        recording_id: str = "",
        recording_title: str = "",
        session_code: str = "",
        meeting_date: str = "",
        grid_layout: Optional[Tuple[int, int]] = None
    ) -> ProctorReport:
        """
        Process a Zoom gallery view recording.

        Args:
            video_path: Path to the video file
            participant_names: List of participant names (in order they appear in grid)
            recording_id: Zoom recording ID
            recording_title: Recording title
            session_code: Session code (e.g., "127")
            meeting_date: Date string (e.g., "12/08")
            grid_layout: (rows, cols) of the gallery grid, auto-detect if None

        Returns:
            ProctorReport with analysis results
        """
        logger.info(f"Processing video: {video_path}")
        logger.info(f"Participants: {len(participant_names)}")

        # Open video
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise ValueError(f"Could not open video: {video_path}")

        # Get video properties
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration_seconds = total_frames / fps if fps > 0 else 0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        logger.info(f"Video: {width}x{height}, {fps:.1f} fps, {duration_seconds:.1f}s, {total_frames} frames")

        # Determine grid layout
        if grid_layout:
            grid_rows, grid_cols = grid_layout
        else:
            grid_rows, grid_cols = self._auto_detect_grid(len(participant_names))

        logger.info(f"Grid layout: {grid_rows}x{grid_cols}")

        # Calculate cell dimensions
        cell_width = width // grid_cols
        cell_height = height // grid_rows

        # Initialize tracking for each participant
        participant_data = {}
        for i, name in enumerate(participant_names):
            row = i // grid_cols
            col = i % grid_cols
            participant_data[name] = {
                "grid_position": (row, col),
                "cell_box": (
                    col * cell_width,
                    row * cell_height,
                    cell_width,
                    cell_height
                ),
                "frame_results": [],  # List of (timestamp, analysis_result)
                "current_violation_start": None,
                "violations": []
            }

        # Process video frames at sample interval
        frame_interval = int(fps * self.sample_interval)
        frame_number = 0
        frames_analyzed = 0

        # Create screenshots directory for this recording
        screenshots_dir = self.output_dir / f"screenshots_{recording_id or 'unknown'}"
        screenshots_dir.mkdir(exist_ok=True)

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Process at sample interval
            if frame_number % frame_interval == 0:
                timestamp_seconds = frame_number / fps
                frames_analyzed += 1

                logger.debug(f"Analyzing frame {frame_number} ({timestamp_seconds:.1f}s)")

                # Analyze each participant's cell
                for name, data in participant_data.items():
                    cell_box = data["cell_box"]
                    analysis = self.face_detector.analyze_participant_cell(frame, cell_box)

                    # Record result
                    data["frame_results"].append((timestamp_seconds, analysis))

                    # Track violations
                    has_issue = len(analysis["issues"]) > 0 and "no_face_detected" in analysis["issues"]

                    if has_issue and data["current_violation_start"] is None:
                        # Start of new violation
                        data["current_violation_start"] = timestamp_seconds

                        # Capture screenshot
                        screenshot_path = self._capture_participant_screenshot(
                            frame, cell_box, name, timestamp_seconds, screenshots_dir
                        )
                        data["current_violation_screenshot"] = screenshot_path

                    elif not has_issue and data["current_violation_start"] is not None:
                        # End of violation
                        violation = ParticipantViolation(
                            participant_name=name,
                            violation_type="no_face",
                            start_time=data["current_violation_start"],
                            end_time=timestamp_seconds,
                            duration=timestamp_seconds - data["current_violation_start"],
                            screenshot_path=data.get("current_violation_screenshot")
                        )
                        data["violations"].append(violation)
                        data["current_violation_start"] = None

            frame_number += 1

        cap.release()

        # Close any open violations at end of video
        for name, data in participant_data.items():
            if data["current_violation_start"] is not None:
                violation = ParticipantViolation(
                    participant_name=name,
                    violation_type="no_face",
                    start_time=data["current_violation_start"],
                    end_time=duration_seconds,
                    duration=duration_seconds - data["current_violation_start"],
                    screenshot_path=data.get("current_violation_screenshot")
                )
                data["violations"].append(violation)

        # Build participant reports
        participant_reports = []
        for name, data in participant_data.items():
            # Calculate visibility stats
            visible_count = sum(
                1 for _, analysis in data["frame_results"]
                if analysis["has_face"]
            )
            total_count = len(data["frame_results"])

            # Count issues
            issues_summary = defaultdict(int)
            for _, analysis in data["frame_results"]:
                for issue in analysis["issues"]:
                    issues_summary[issue] += 1

            report = ParticipantReport(
                name=name,
                grid_position=data["grid_position"],
                total_frames=total_count,
                visible_frames=visible_count,
                visibility_percentage=(visible_count / total_count * 100) if total_count > 0 else 0,
                violations=data["violations"],
                issues_summary=dict(issues_summary)
            )
            participant_reports.append(report)

        # Build full report
        proctor_report = ProctorReport(
            recording_id=recording_id,
            recording_title=recording_title,
            session_code=session_code,
            meeting_date=meeting_date,
            processing_date=datetime.now().isoformat(),
            total_duration_seconds=duration_seconds,
            frames_analyzed=frames_analyzed,
            sample_interval_seconds=self.sample_interval,
            participants=participant_reports,
            screenshots_dir=str(screenshots_dir)
        )

        logger.info(f"Processing complete: {frames_analyzed} frames analyzed")
        logger.info(f"Participants with violations: {sum(1 for p in participant_reports if p.violations)}")

        return proctor_report

    def _auto_detect_grid(self, participant_count: int) -> Tuple[int, int]:
        """
        Auto-detect grid layout based on participant count.

        Zoom typically uses layouts like:
        - 1 participant: 1x1
        - 2-4 participants: 2x2
        - 5-9 participants: 3x3
        - 10-16 participants: 4x4
        - etc.
        """
        import math

        if participant_count <= 1:
            return (1, 1)
        elif participant_count <= 4:
            return (2, 2)
        elif participant_count <= 9:
            return (3, 3)
        elif participant_count <= 16:
            return (4, 4)
        elif participant_count <= 25:
            return (5, 5)
        elif participant_count <= 36:
            return (6, 6)
        else:
            cols = math.ceil(math.sqrt(participant_count))
            rows = math.ceil(participant_count / cols)
            return (rows, cols)

    def _capture_participant_screenshot(
        self,
        frame: any,
        cell_box: Tuple[int, int, int, int],
        participant_name: str,
        timestamp: float,
        output_dir: Path
    ) -> str:
        """Capture and save a screenshot of a participant's cell."""
        x, y, w, h = cell_box

        # Extract cell region
        cell_frame = frame[y:y+h, x:x+w]

        # Generate filename
        safe_name = "".join(c if c.isalnum() else "_" for c in participant_name)
        filename = f"{safe_name}_{timestamp:.0f}s.jpg"
        filepath = output_dir / filename

        # Save screenshot
        cv2.imwrite(str(filepath), cell_frame)

        return str(filepath)

    def generate_warning_document(
        self,
        report: ProctorReport,
        participant_name: str,
        min_violation_duration: float = 60.0  # Only include violations > 1 minute
    ) -> Dict:
        """
        Generate a warning document for a specific participant.

        Args:
            report: Full proctor report
            participant_name: Name of participant to generate warning for
            min_violation_duration: Minimum violation duration to include (seconds)

        Returns:
            Document data with text and screenshots
        """
        # Find participant in report
        participant = None
        for p in report.participants:
            if p.name == participant_name:
                participant = p
                break

        if not participant:
            raise ValueError(f"Participant '{participant_name}' not found in report")

        # Filter significant violations
        significant_violations = [
            v for v in participant.violations
            if v.duration >= min_violation_duration
        ]

        if not significant_violations:
            return {
                "has_violations": False,
                "participant_name": participant_name,
                "message": "No significant video visibility issues detected."
            }

        # Build warning document
        total_violation_time = sum(v.duration for v in significant_violations)

        document = {
            "has_violations": True,
            "participant_name": participant_name,
            "session_code": report.session_code,
            "meeting_date": report.meeting_date,
            "meeting_duration_minutes": report.total_duration_seconds / 60,
            "visibility_percentage": participant.visibility_percentage,
            "total_violation_minutes": total_violation_time / 60,
            "violation_count": len(significant_violations),
            "violations": [],
            "screenshots": []
        }

        # Add violation details
        for v in significant_violations:
            violation_data = {
                "type": v.violation_type,
                "start_time": self._format_timestamp(v.start_time),
                "end_time": self._format_timestamp(v.end_time),
                "duration_minutes": v.duration / 60
            }
            document["violations"].append(violation_data)

            # Include screenshot as base64
            if v.screenshot_path and os.path.exists(v.screenshot_path):
                with open(v.screenshot_path, "rb") as f:
                    screenshot_b64 = base64.b64encode(f.read()).decode("utf-8")
                    document["screenshots"].append({
                        "timestamp": self._format_timestamp(v.start_time),
                        "data": screenshot_b64,
                        "filename": os.path.basename(v.screenshot_path)
                    })

        # Generate summary text
        document["summary_text"] = self._generate_warning_text(document)

        return document

    def _format_timestamp(self, seconds: float) -> str:
        """Format seconds as HH:MM:SS."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes}:{secs:02d}"

    def _generate_warning_text(self, document: Dict) -> str:
        """Generate warning text for a participant."""
        text = f"""
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
            text += f"""
{i}. {v['type'].replace('_', ' ').title()}
   Time: {v['start_time']} - {v['end_time']} ({v['duration_minutes']:.1f} min)
"""

        text += """
REMINDER:
Students are required to have their camera on and face visible throughout
the session. Please ensure your camera is working properly and you remain
visible on screen for future sessions.

If you believe this is an error, please contact your instructor.
"""
        return text

    def save_report_json(self, report: ProctorReport, output_path: str = None) -> str:
        """Save report as JSON file."""
        if output_path is None:
            output_path = self.output_dir / f"report_{report.recording_id or 'unknown'}.json"

        # Convert to dict
        report_dict = {
            "recording_id": report.recording_id,
            "recording_title": report.recording_title,
            "session_code": report.session_code,
            "meeting_date": report.meeting_date,
            "processing_date": report.processing_date,
            "total_duration_seconds": report.total_duration_seconds,
            "frames_analyzed": report.frames_analyzed,
            "sample_interval_seconds": report.sample_interval_seconds,
            "screenshots_dir": report.screenshots_dir,
            "participants": []
        }

        for p in report.participants:
            p_dict = {
                "name": p.name,
                "grid_position": p.grid_position,
                "total_frames": p.total_frames,
                "visible_frames": p.visible_frames,
                "visibility_percentage": p.visibility_percentage,
                "issues_summary": p.issues_summary,
                "violations": []
            }
            for v in p.violations:
                p_dict["violations"].append({
                    "violation_type": v.violation_type,
                    "start_time": v.start_time,
                    "end_time": v.end_time,
                    "duration": v.duration,
                    "screenshot_path": v.screenshot_path
                })
            report_dict["participants"].append(p_dict)

        with open(output_path, "w") as f:
            json.dump(report_dict, f, indent=2)

        logger.info(f"Report saved to {output_path}")
        return str(output_path)

    def cleanup(self):
        """Release resources."""
        if self.face_detector:
            self.face_detector.cleanup()
