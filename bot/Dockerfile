# Self-hosted Live Tutor bot. The Playwright base image ships Chromium + the
# system libraries WebRTC needs, which is the hard part of running a meeting bot
# in a container.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY bot/requirements.txt ./bot/requirements.txt
RUN pip install --no-cache-dir -r bot/requirements.txt \
    && python -m playwright install chromium

COPY bot/ ./bot/

ENV BOT_HEADLESS=true \
    BOT_PUBLIC_BASE_URL=http://localhost:8088

EXPOSE 8088

# uvicorn serves the contract + the Zoom client page. Bind Render's $PORT
# (falls back to 8088 for local Docker runs).
CMD ["sh", "-c", "uvicorn bot.app:app --host 0.0.0.0 --port ${PORT:-8088}"]
