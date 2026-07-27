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
# CDN serves the same bytes as zoom-meeting-embedded-<version>.min.js
# (both are exactly 3,291,075 bytes for 3.13.2). The version is in the
# tarball URL, not the file inside it.
ARG ZOOM_SDK_VERSION=3.13.2
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
CMD ["sh", "-c", "uvicorn bot.app:app --host 0.0.0.0 --port ${PORT:-8088}"]
