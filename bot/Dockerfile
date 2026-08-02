# Self-hosted Live Tutor bot. The Playwright base image ships Chromium + the
# system libraries WebRTC needs, which is the hard part of running a meeting bot
# in a container.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY bot/requirements.txt ./bot/requirements.txt
RUN pip install --no-cache-dir -r bot/requirements.txt \
    && python -m playwright install chromium

COPY bot/ ./bot/

# Vendor the Zoom Web Meeting SDK into the image rather than pulling it
# from source.zoom.us at join time. The CDN copy loads fine from a normal
# browser, but inside this container it never defined ZoomMtgEmbedded,
# and from a page served with Cross-Origin-Embedder-Policy: require-corp
# there are several ways for a cross-origin script to be dropped without
# a usable error. Serving it same-origin removes the CDN, the CORS
# question and the COEP question in one move, and matches how the
# platform already self-hosts MediaPipe rather than trusting a CDN at
# runtime.
#
# The npm tarball carries dist/lib alongside the SDK: the audio/video
# wasm and workers the SDK fetches once a meeting starts. Shipping only
# the .js would move the failure later instead of fixing it.
#
# Note the filename. npm ships zoom-meeting-embedded-ES5.min.js while the
# CDN serves the same bytes as zoom-meeting-embedded-<version>.min.js.
# The version is in the tarball URL, not the file inside it.
#
# 6.x, not 3.13.2: private messages sent from the 3.x SDK reach Zoom but
# Zoom Workplace clients running continuous meeting chat refuse to render
# them ("Messages can't be synced to this meeting chat"), which made every
# direct message silently vanish on the receiving device. The page's method
# surface was runtime-verified identical on 3.13.2 and 6.2.0.
# MediaPipe face detection, vendored the same way for the same reasons.
# It powers the in-page seat watcher: detection runs inside the browser
# that is already decoding the video, so every rendered seat is checked
# about once a second, instead of one screenshot at a time via Python.
# The short-range model is built for webcam-distance faces.
ARG MEDIAPIPE_FD_VERSION=0.4.1646425229
RUN set -eux; \
    mkdir -p /tmp/mpfd bot/static/vendor/mediapipe; \
    curl -fsSL "https://registry.npmjs.org/@mediapipe/face_detection/-/face_detection-${MEDIAPIPE_FD_VERSION}.tgz" \
      -o /tmp/mpfd.tgz; \
    tar -xzf /tmp/mpfd.tgz -C /tmp/mpfd; \
    cp /tmp/mpfd/package/face_detection.js \
       /tmp/mpfd/package/face_detection_short.binarypb \
       /tmp/mpfd/package/face_detection_short_range.tflite \
       /tmp/mpfd/package/face_detection_solution_simd_wasm_bin.js \
       /tmp/mpfd/package/face_detection_solution_simd_wasm_bin.wasm \
       /tmp/mpfd/package/face_detection_solution_simd_wasm_bin.data \
       /tmp/mpfd/package/face_detection_solution_wasm_bin.js \
       /tmp/mpfd/package/face_detection_solution_wasm_bin.wasm \
       bot/static/vendor/mediapipe/; \
    rm -rf /tmp/mpfd /tmp/mpfd.tgz; \
    test -f bot/static/vendor/mediapipe/face_detection.js

ARG ZOOM_SDK_VERSION=6.2.0
RUN set -eux; \
    mkdir -p /tmp/zsdk bot/static/vendor; \
    curl -fsSL "https://registry.npmjs.org/@zoom/meetingsdk/-/meetingsdk-${ZOOM_SDK_VERSION}.tgz" \
      -o /tmp/zsdk.tgz; \
    tar -xzf /tmp/zsdk.tgz -C /tmp/zsdk; \
    cp -r /tmp/zsdk/package/dist/. bot/static/vendor/; \
    rm -rf /tmp/zsdk /tmp/zsdk.tgz; \
    test -f bot/static/vendor/zoom-meeting-embedded-ES5.min.js

# BOT_PUBLIC_BASE_URL is deliberately NOT set here. Baking in localhost
# meant config.py's RENDER_EXTERNAL_URL fallback could never take effect,
# so on Render the headless browser tried to load the Zoom client page
# from http://localhost:8088 and got ERR_CONNECTION_REFUSED. Leave it
# unset and let config.py fall back, or set it explicitly per deploy.
ENV BOT_HEADLESS=true

EXPOSE 8088

# uvicorn serves the contract + the Zoom client page. Bind Render's $PORT
# (falls back to 8088 for local Docker runs).
#
# --timeout-graceful-shutdown: without a cap, uvicorn waits forever for open
# requests on SIGTERM. A container wedged by a Chromium pile-up has requests
# that never finish, so it sat there refusing to die while Render kept
# routing traffic to it, and the healthy replacement never took over. Twenty
# seconds is more than any legitimate request here needs.
CMD ["sh", "-c", "uvicorn bot.app:app --host 0.0.0.0 --port ${PORT:-8088} --timeout-graceful-shutdown 20"]
