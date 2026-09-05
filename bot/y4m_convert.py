"""Turn a picture into a two-frame Y4M file for Chromium's fake webcam.

Runs as its own short-lived process, on purpose. The conversion needs
OpenCV and NumPy, which together hold about 38 MB once imported, and a
lookout that will never show the picture (the memory gate declines it in
a full class) used to pay that for nothing, for the whole session, inside
the bot's main process. Done here, the cost lives for a second in a child
and is gone. No relative imports: this file is run by path.

Usage: python y4m_convert.py <image_path> <out.y4m> <width> <height> <fps>
Exit code 0 on success, 1 when the image will not decode.
"""
import sys


def convert(in_path: str, out_path: str, width: int, height: int, fps: int) -> bool:
    import numpy as np
    import cv2
    with open(in_path, "rb") as f:
        data = f.read()
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return False
    h, w = img.shape[:2]
    scale = min(width / w, height / h)
    nw, nh = max(2, int(round(w * scale))), max(2, int(round(h * scale)))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:] = (20, 20, 12)
    y0, x0 = (height - nh) // 2, (width - nw) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    # I420 is exactly the planar Y, U, V layout a C420 Y4M frame wants.
    frame = cv2.cvtColor(canvas, cv2.COLOR_BGR2YUV_I420).tobytes()
    with open(out_path, "wb") as f:
        f.write(f"YUV4MPEG2 W{width} H{height} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for _ in range(2):
            f.write(b"FRAME\n" + frame)
    return True


if __name__ == "__main__":
    try:
        in_path, out_path, w, h, fps = sys.argv[1:6]
        ok = convert(in_path, out_path, int(w), int(h), int(fps))
    except Exception:
        ok = False
    sys.exit(0 if ok else 1)
