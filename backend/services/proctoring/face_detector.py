"""
Face Detection Service

Uses MediaPipe or face_recognition library to detect faces in video frames.
Adapted from CIA repository's face-monitor approach for server-side video processing.
"""

import cv2
import numpy as np
from typing import List, Dict, Tuple, Optional
import logging

logger = logging.getLogger(__name__)


class FaceDetector:
    """
    Face detection wrapper that works with video frames.

    Supports multiple backends:
    1. MediaPipe (fast, good for real-time)
    2. face_recognition (more accurate, slower)
    3. OpenCV Haar Cascade (fallback)
    """

    def __init__(self, backend: str = "opencv"):
        """
        Initialize face detector.

        Args:
            backend: Detection backend ("mediapipe", "face_recognition", "opencv")
        """
        self.backend = backend
        self.detector = None
        self._initialize()

    def _initialize(self):
        """Initialize the selected backend."""
        if self.backend == "mediapipe":
            self._init_mediapipe()
        elif self.backend == "face_recognition":
            self._init_face_recognition()
        else:
            self._init_opencv()

    def _init_mediapipe(self):
        """Initialize MediaPipe face detection."""
        try:
            import mediapipe as mp
            self.mp_face_detection = mp.solutions.face_detection
            self.detector = self.mp_face_detection.FaceDetection(
                model_selection=1,  # 0 for short-range, 1 for full-range
                min_detection_confidence=0.5
            )
            logger.info("MediaPipe face detector initialized")
        except ImportError:
            logger.warning("MediaPipe not available, falling back to OpenCV")
            self._init_opencv()

    def _init_face_recognition(self):
        """Initialize face_recognition library."""
        try:
            import face_recognition
            self.face_recognition = face_recognition
            self.detector = "face_recognition"
            logger.info("face_recognition detector initialized")
        except ImportError:
            logger.warning("face_recognition not available, falling back to OpenCV")
            self._init_opencv()

    def _init_opencv(self):
        """Initialize OpenCV Haar Cascade (fallback)."""
        self.backend = "opencv"
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        self.detector = cv2.CascadeClassifier(cascade_path)
        logger.info("OpenCV Haar Cascade face detector initialized")

    def detect_faces(self, frame: np.ndarray) -> List[Dict]:
        """
        Detect faces in a video frame.

        Args:
            frame: BGR image as numpy array (from cv2.imread or cv2.VideoCapture)

        Returns:
            List of detected faces with bounding boxes:
            [{"box": (x, y, w, h), "confidence": float, "center": (cx, cy)}]
        """
        if frame is None or frame.size == 0:
            return []

        if self.backend == "mediapipe":
            return self._detect_mediapipe(frame)
        elif self.backend == "face_recognition":
            return self._detect_face_recognition(frame)
        else:
            return self._detect_opencv(frame)

    def _detect_mediapipe(self, frame: np.ndarray) -> List[Dict]:
        """Detect faces using MediaPipe."""
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.detector.process(rgb_frame)

        faces = []
        if results.detections:
            h, w = frame.shape[:2]
            for detection in results.detections:
                bbox = detection.location_data.relative_bounding_box
                x = int(bbox.xmin * w)
                y = int(bbox.ymin * h)
                width = int(bbox.width * w)
                height = int(bbox.height * h)

                # Ensure bounds are valid
                x = max(0, x)
                y = max(0, y)
                width = min(width, w - x)
                height = min(height, h - y)

                faces.append({
                    "box": (x, y, width, height),
                    "confidence": detection.score[0],
                    "center": (x + width // 2, y + height // 2)
                })

        return faces

    def _detect_face_recognition(self, frame: np.ndarray) -> List[Dict]:
        """Detect faces using face_recognition library."""
        # face_recognition expects RGB
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Detect face locations (returns list of (top, right, bottom, left))
        face_locations = self.face_recognition.face_locations(rgb_frame, model="hog")

        faces = []
        for (top, right, bottom, left) in face_locations:
            x = left
            y = top
            width = right - left
            height = bottom - top

            faces.append({
                "box": (x, y, width, height),
                "confidence": 1.0,  # face_recognition doesn't provide confidence
                "center": (x + width // 2, y + height // 2)
            })

        return faces

    def _detect_opencv(self, frame: np.ndarray) -> List[Dict]:
        """Detect faces using OpenCV Haar Cascade."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Detect faces
        detections = self.detector.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 30),
            flags=cv2.CASCADE_SCALE_IMAGE
        )

        faces = []
        for (x, y, w, h) in detections:
            faces.append({
                "box": (x, y, w, h),
                "confidence": 0.8,  # Haar doesn't provide confidence
                "center": (x + w // 2, y + h // 2)
            })

        return faces

    def detect_faces_in_grid(
        self,
        frame: np.ndarray,
        grid_rows: int,
        grid_cols: int
    ) -> Dict[Tuple[int, int], List[Dict]]:
        """
        Detect faces and map them to grid positions (for gallery view).

        In Zoom gallery view, participants are arranged in a grid.
        This method detects all faces and assigns them to grid cells.

        Args:
            frame: Video frame
            grid_rows: Number of rows in participant grid
            grid_cols: Number of columns in participant grid

        Returns:
            Dict mapping (row, col) to list of faces in that cell
        """
        h, w = frame.shape[:2]
        cell_width = w // grid_cols
        cell_height = h // grid_rows

        # Detect all faces in frame
        all_faces = self.detect_faces(frame)

        # Assign faces to grid cells
        grid_faces = {}
        for row in range(grid_rows):
            for col in range(grid_cols):
                grid_faces[(row, col)] = []

        for face in all_faces:
            cx, cy = face["center"]
            # Determine which grid cell this face belongs to
            col = min(cx // cell_width, grid_cols - 1)
            row = min(cy // cell_height, grid_rows - 1)
            grid_faces[(row, col)].append(face)

        return grid_faces

    def analyze_participant_cell(
        self,
        frame: np.ndarray,
        cell_box: Tuple[int, int, int, int]
    ) -> Dict:
        """
        Analyze a single participant's cell in gallery view.

        Args:
            frame: Full video frame
            cell_box: (x, y, width, height) of the participant's cell

        Returns:
            Analysis result with face detection info
        """
        x, y, w, h = cell_box

        # Extract cell region
        cell_frame = frame[y:y+h, x:x+w]

        if cell_frame.size == 0:
            return {
                "has_face": False,
                "face_count": 0,
                "faces": [],
                "visibility_score": 0.0,
                "issues": ["empty_cell"]
            }

        # Detect faces in cell
        faces = self.detect_faces(cell_frame)
        face_count = len(faces)

        # Analyze visibility
        issues = []
        visibility_score = 0.0

        if face_count == 0:
            issues.append("no_face_detected")
            visibility_score = 0.0
        elif face_count == 1:
            face = faces[0]
            # Check if face is reasonably sized (not too small)
            face_area = face["box"][2] * face["box"][3]
            cell_area = w * h
            face_ratio = face_area / cell_area

            if face_ratio < 0.02:  # Face is less than 2% of cell
                issues.append("face_too_small")
                visibility_score = 0.3
            elif face_ratio > 0.5:  # Face is more than 50% (too close)
                issues.append("face_too_close")
                visibility_score = 0.7
            else:
                visibility_score = min(1.0, face["confidence"])
        else:
            issues.append("multiple_faces")
            visibility_score = 0.5  # Partial credit but flagged

        return {
            "has_face": face_count > 0,
            "face_count": face_count,
            "faces": faces,
            "visibility_score": visibility_score,
            "issues": issues
        }

    def cleanup(self):
        """Release resources."""
        if self.backend == "mediapipe" and hasattr(self, 'detector') and self.detector:
            self.detector.close()
