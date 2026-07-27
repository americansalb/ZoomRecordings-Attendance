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
- **Attendance from participant identity, not tile position.** The browser page
  keeps a presence ledger keyed by Zoom user id, driven by Zoom's own
  `user-added` / `user-removed` / `user-updated` events, so it records who was
  in the room, from when to when, and how long each camera was actually on.
- **Attendance does not depend on screenshots.** The capture toggle decides
  whether pixels are kept, not whether attendance is taken. Attendance that
  switches itself off when a privacy setting is enabled is not attendance.
- **Two cross-checking signals.** Each record carries `video_on` (from Zoom's
  per-user state, authoritative) and, when a frame is available, `face_present`
  (OpenCV) as a corroborating check.
- **All-Python orchestration**, reusing your OpenCV + Google stack. The native
  C++ Meeting SDK is a future upgrade behind the same `MeetingClient` interface.

## Known limitation: per-user frame capture

Per-user video frames are **not obtainable** with the Component View Meeting
SDK. The design this bot was originally built to assumed
`getMediaStream().renderVideo(canvas, userId, ...)`, which is a **Video SDK**
API. The object returned by `ZoomMtgEmbedded.createClient()` has no
`getMediaStream` at all, in 3.13.2 or in 6.2.0, and the Video SDK cannot join
ordinary Zoom meetings.

`zoomCaptureUser()` therefore returns `null` and `zoomCaptureSupported()`
returns `false`. `face_present` is recorded only when a frame genuinely exists,
and `face_checked` distinguishes "no face" from "never looked". Attendance is
unaffected: `video_on` plus presence duration is the signal the participation
rule actually needs.

To get frames you would need a different capture path entirely, such as a
native Meeting SDK bot or a cloud-recording pass, not a change to this file.

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

## The integration surface
`static/zoom_client.js` targets the Zoom Web SDK **Component View**, vendored
into the image at the version pinned by `ZOOM_SDK_VERSION` in `bot/Dockerfile`.

The method names below were verified against the object `createClient()`
actually returns (67 methods). A lot of Zoom sample code online uses names that
belong to the Video SDK or to old releases and that simply do not exist here:

| Used | Do **not** use |
|---|---|
| `client.sendChat(text, userId?)` | `client.getChatClient().send(...)`, `sendToAll(...)` |
| `client.getAttendeeslist()` | `client.getAllUser()` |
| `client.leaveMeeting()` | `client.leave()` |
| DOM/event based presence | `client.getMediaStream().renderVideo(...)` |

Calling any of the right-hand names throws a `TypeError`. When that happened
inside `zoomJoin` after a successful `client.join()`, the bot ended up parked
silently in the meeting with the backend believing the join had failed.

`init()` must be given `assetPath` pointing at the vendored `/lib/av`, otherwise
the SDK fetches its media wasm from `source.zoom.us` at join time, which this
page's `Cross-Origin-Embedder-Policy: require-corp` is there to avoid.

## Privacy
Capture is **off by default** and announces the bot on join. These are students'
faces (likely minors) — enable capture only with a consent/policy basis, and
consider the backend's "presence flags only" mode (`store_images:false`) if you
don't need to retain images.

## What's tested
`bot/tests/test_bot.py` covers the orchestration with fakes:

- the SDK signature
- the attendance pipeline: presence durations, video-on/off, face
  present/absent, store vs log-only, and attendance recorded correctly **with
  no frames at all** (the real SDK's behaviour)
- the bot skipping itself by user id rather than display name, so a student who
  shares the bot's name is still counted
- the HTTP contract: join returns a runtime_id, announce, inbound chat becomes a
  backend event, send, leave
- a failed join tearing the browser down and reporting the reason, instead of
  leaving a ghost participant in the meeting
- the meeting ending on Zoom's side producing a `left` event and reaping the
  session
- passcode and ZAK reaching the meeting client, including the passcode carried
  in a join URL

Run with `python -m bot.tests.test_bot`. The live Zoom join and chat path still
needs your SDK credentials and a real meeting to exercise.
