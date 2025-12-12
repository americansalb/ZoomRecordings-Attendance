# Video Proctoring Service
# Processes Zoom gallery view recordings to check participant video presence

from .video_processor import VideoProctorService
from .face_detector import FaceDetector

__all__ = ['VideoProctorService', 'FaceDetector']
