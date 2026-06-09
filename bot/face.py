"""
Face-presence check (OpenCV Haar cascade).

Used as the *secondary* signal in attribution: identity comes from Zoom's
per-user stream (we always know whose frame it is), and this answers "is a face
actually visible in that frame right now?". Camera-off frames return False; the
identity is still recorded by the caller.

Kept dependency-light and defensive: any failure returns False rather than
raising, so the capture loop never dies on a bad frame.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_cascade = None


def _get_cascade():
    global _cascade
    if _cascade is None:
        import cv2  # lazy: only needed when actually analyzing frames
        _cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    return _cascade


def face_present(image_bytes: bytes) -> bool:
    """True if at least one face is detected in the PNG/JPEG bytes."""
    if not image_bytes:
        return False
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = _get_cascade().detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
        )
        return len(faces) > 0
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("face_present check failed: %s", e)
        return False
