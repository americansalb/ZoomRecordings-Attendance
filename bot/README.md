# Live Tutor bot (self-hosted Zoom Meeting bot)

This is the bot that actually joins live Zoom meetings. The Phase 1 backend
drives it over the HTTP contract in [`../TUTOR_BOT.md`](../TUTOR_BOT.md); the bot
runs the Zoom **Web Meeting SDK** inside a headless Chromium (via Playwright),
sends/receives chat, and — when enabled — captures a periodic per-student
snapshot, attributes it by Zoom identity, and files it to Google Drive.

```
backend ──HTTP (TUTOR_BOT.md)──► bot/app.py ──Playwright──► Chromium + Zoom Web SDK ──► meeting
   ▲                                  │
   └────── chat events ───────────────┤  POST /api/tutor/bot/events
   └────── screenshot manifest ───────┘  POST /api/tutor/bot/screenshots
                                          frames ──► Google Drive (per session)
```

## Why this design
- **Per-user capture, not gallery screenshots.** `zoomCaptureUser(userId)` renders
  *one* student's own video to our off-screen canvas, so attribution is by Zoom
  user id and **tile position is irrelevant** (your requirement).
- **Two cross-checking signals.** Each snapshot records `video_on` (from Zoom's
  per-user state) and `face_present` (OpenCV). Camera off ⇒ `video_on:false`;
  camera on but no face ⇒ `face_present:false`. Either is a failsafe for the other.
- **All-Python orchestration**, reusing your OpenCV + Google stack. The native
  C++ Meeting SDK is a future upgrade behind the same `MeetingClient` interface.

## Prerequisites
- A Zoom **Meeting SDK** app (Marketplace → Build App → *Meeting SDK*). This gives
  you an **SDK Key/Secret** — different from your Server-to-Server OAuth creds, and
  required to authorize a join. Your account must permit the SDK/bot to join.
- A Google service account with Drive access (reuse the one the main app uses).

## Configuration

On Render, the Blueprint (`render.yaml`) wires the two services together, so the
only values you paste are the Zoom keys (and Google creds, for screenshots).

**You paste these:**

| Env var | Purpose |
|---|---|
| `ZOOM_MEETING_SDK_KEY` / `ZOOM_MEETING_SDK_SECRET` | Meeting SDK app credentials (Client ID + Secret). |
| `GOOGLE_CLIENT_EMAIL` + `GOOGLE_PRIVATE_KEY` (or `GOOGLE_SERVICE_ACCOUNT_FILE`) | Drive credentials for screenshot uploads. |
| `TUTOR_DRIVE_FOLDER_ID` | (optional) Parent Drive folder for per-session screenshot folders. |

**Auto-set on Render (set by hand only for local dev):**

| Env var | Purpose |
|---|---|
| `BACKEND_URL` | Backend base URL. On Render, wired from the backend service. A bare hostname is fine — the bot adds `https://`. |
| `TUTOR_BOT_SHARED_SECRET` | Shared secret. Generated once and shared with the backend via an env group. |
| `BOT_PUBLIC_BASE_URL` | URL the headless browser loads the client page from. Defaults to Render's `RENDER_EXTERNAL_URL`. |
| `BOT_HEADLESS` | `true` (default) or `false` to debug with a visible browser. |

The backend side (`TUTOR_BOT_BASE_URL`, `TUTOR_BOT_SHARED_SECRET`,
`ANTHROPIC_API_KEY`) is likewise wired by the Blueprint — see `render.yaml`.

## Run

Docker (recommended — the Playwright base image bundles Chromium + WebRTC libs):
```bash
docker build -f bot/Dockerfile -t livetutor-bot .
docker run -p 8088:8088 --env-file bot/.env livetutor-bot
```

Local:
```bash
pip install -r bot/requirements.txt
python -m playwright install chromium
uvicorn bot.app:app --host 0.0.0.0 --port 8088
```

## The one piece you must validate live
`static/zoom_client.js` targets the Zoom Web SDK **Component View**. SDK method
names shift between versions, so pin the SDK `<script>` in `static/zoom_client.html`
to the version you built against and confirm these calls against the docs:
`createClient/init/join`, `getChatClient().send/sendToAll`, `getAttendeeslist`,
and `getMediaStream().renderVideo` (the capture primitive). It's the only
integration surface — the four `window.zoom*` functions and `window.onZoomChat`.

## Privacy
Capture is **off by default** and announces the bot on join. These are students'
faces (likely minors) — enable capture only with a consent/policy basis, and
consider the backend's "presence flags only" mode (`store_images:false`) if you
don't need to retain images.

## What's tested
`bot/tests/test_bot.py` covers the orchestration with fakes: the SDK signature,
the capture→attribute→manifest pipeline (video-on/off, face present/absent,
store vs log-only, bot-self skipped), and the HTTP contract (join → runtime_id,
announce, inbound chat → backend event, send, leave). The live Zoom join /
chat / renderVideo path requires your SDK creds and a real meeting to exercise.
