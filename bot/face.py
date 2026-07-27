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


def _decode_scaled(image_bytes: bytes):
    """Decode, downscaling anything wider than 800px.

    The capture path can hand this a full 1280 wide gallery frame, and Haar
    over that costs seconds per tick, which silently stretches the observation
    interval. Faces stay comfortably above the 30px detection floor at 800.
    Returns (image, scale) or (None, 1.0).
    """
    import cv2
    import numpy as np

    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, 1.0
    width = img.shape[1]
    if width <= 800:
        return img, 1.0
    scale = 800.0 / width
    resized = cv2.resize(img, (800, max(1, int(img.shape[0] * scale))))
    return resized, scale


def face_present(image_bytes: bytes) -> bool:
    """True if at least one face is detected in the PNG/JPEG bytes."""
    if not image_bytes:
        return False
    try:
        import cv2

        img, _ = _decode_scaled(image_bytes)
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


def face_check_annotated(image_bytes: bytes):
    """Run the exact production face check and draw what it found.

    Returns (found, annotated_png_bytes). This exists so an operator can SEE
    what the detector considers a face instead of arguing with a boolean: same
    cascade, same parameters, with a rectangle around every hit.
    """
    if not image_bytes:
        return False, None
    try:
        import cv2

        img, _ = _decode_scaled(image_bytes)
        if img is None:
            return False, None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = _get_cascade().detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
        )
        for (x, y, w, h) in faces:
            cv2.rectangle(img, (x, y), (x + w, y + h), (63, 141, 176), 3)
        ok, buf = cv2.imencode(".png", img)
        return len(faces) > 0, (buf.tobytes() if ok else None)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("face_check_annotated failed: %s", e)
        return False, None
