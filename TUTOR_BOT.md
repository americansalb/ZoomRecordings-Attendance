# Live Tutor — bot integration contract

The Live Tutor backend never talks to the Zoom Meeting SDK directly. That lives
in **your self-hosted bot process**. The backend drives it over a small HTTP
contract and receives chat/lifecycle events back on a webhook. This file is the
spec your bot must implement.

```
 React admin ──► FastAPI backend ──HTTP──► your bot ──Meeting SDK──► Zoom meeting
                       ▲                       │
                       └──────webhook──────────┘   (incoming chat, lifecycle)
```

Set `TUTOR_BOT_BASE_URL` to your bot's control API. If it is unset the backend
runs in **null-bot mode**: it logs what it *would* send but sends nothing — so
the admin UI, policies, drafting, and the approval queue are all testable
without a live meeting.

If `TUTOR_BOT_SHARED_SECRET` is set, the backend adds it as the
`X-Tutor-Bot-Secret` header on every outbound call, and requires the same header
on inbound webhook posts.

---

## Outbound: backend → your bot

All requests carry `Content-Type: application/json` and (if configured)
`X-Tutor-Bot-Secret: <secret>`.

### 1. Join a meeting — `POST {base}/bots`

```json
{
  "meeting_id": "98765432101",
  "meeting_uuid": "abcd==/EF…",     // may be null
  "join_url": null,                  // may be null; use meeting_id if absent
  "display_name": "AALB Assistant",
  "announce": true,
  "announcement": "Hi everyone — I'm the AALB assistant…",  // post on join if announce
  "session_ref": "42",               // OPAQUE: echo this back on every event
  "passcode": null,                  // meeting passcode, if the meeting has one
  "zak": null,                       // host ZAK, to join as an authenticated user
  "role": null,                      // override the signed role (0 attendee, 1 host)
  "capture": {                        // per-student screenshot config (may be null)
    "enabled": false,
    "interval_seconds": 300,
    "store_images": true
  }
}
```

`passcode` is required for any meeting that has one; without it Zoom rejects the
join. If it is absent the bot falls back to the `pwd` parameter in `join_url`.

`capture` controls **screenshots only**. Attendance is reported on every tick
regardless of it, because attendance that switches itself off when a privacy
setting is enabled is not attendance.

`role` defaults to 1 when a ZAK is present and 0 otherwise. Override it when the
ZAK belongs to a user who is neither host nor alternative host, since signing
role 1 for such a user is rejected.

A join can take well over a minute: the bot launches a browser, loads the Zoom
SDK and then completes the Zoom join. Allow at least 150 seconds before treating
it as failed, or you will record a successful join as an error and be left
unable to message or dismiss a bot that is really in the meeting.

Respond `200` with the bot's own handle:

```json
{ "runtime_id": "bot_01HXYZ…" }
```

`runtime_id` is whatever id your bot uses to address this meeting later. The
backend stores it and uses it for sends/leave. (`id` is also accepted.)

If `announce` is true, post `announcement` to public chat once the bot is in.

### 2. Send a message — `POST {base}/bots/{runtime_id}/messages`

```json
{
  "channel": "public",              // "public" | "dm"
  "text": "Class starts at 6 pm ET.",
  "to_participant_id": null          // required when channel == "dm"
}
```

Respond `200` on success, non-2xx on failure (the backend marks the send failed
and surfaces the error to the admin).

### 3. Leave a meeting — `DELETE {base}/bots/{runtime_id}`

Respond `200`. The bot should leave the meeting and release resources.

---

## Inbound: your bot → backend webhook

Your bot POSTs events to:

```
POST {BACKEND}/api/tutor/bot/events
Header: X-Tutor-Bot-Secret: <secret>   (if configured)
```

Every event should include **either** `session_ref` (the opaque value from the
join request — preferred) **or** `runtime_id`, so the backend can map the event
to the right session.

### Incoming chat — the important one

```json
{
  "type": "chat",
  "session_ref": "42",
  "runtime_id": "bot_01HXYZ…",
  "channel": "public",               // "public" | "dm"
  "participant_id": "p_55",          // needed to reply to a DM
  "participant_name": "Sam R.",
  "text": "when does class start?"
}
```

What the backend does with it:

- The message is **always logged** (inbound).
- If the relevant capability is on (`answer_questions` for public, `direct_messages`
  for DMs) **and** guardrails pass (cooldown, per-session cap, not quiet mode),
  the message is sent to Opus 4.8, which drafts a policy-compliant reply **or
  abstains**.
- A draft never auto-sends. It lands in the **approval queue**. A human approves
  (optionally editing) before your bot is asked to send it.

So your bot does **not** need to wait for or correlate a response to a chat
event — replies arrive later as ordinary `POST /messages` calls after a human
approves them.

### Lifecycle events

```json
{ "type": "joined",  "session_ref": "42", "runtime_id": "bot_01HXYZ…" }
{ "type": "left",    "session_ref": "42", "runtime_id": "bot_01HXYZ…" }
{ "type": "error",   "session_ref": "42", "error": "failed to join: waiting room" }
```

- `joined` / `ready` → session marked `in_meeting`. Sent once the bot is in.
- `left` / `ended` → session marked `left`. Sent when the meeting ends on Zoom's
  side, at which point the bot also tears its own browser down.
- `error` → session marked `error` with the message. Sent when a join fails,
  **after** the bot has closed the browser, so a failed join never leaves a
  silent participant behind.
- Anything else (e.g. `participant_joined`) is accepted and ignored for now.

An errored session leaves the active states immediately, so it disappears from
`GET /api/tutor/sessions`. Pass `include_finished=true` to see it and read the
error text.

Respond `200` to acknowledge; the backend returns `{ "success": true }`.

---

## Attendance

The bot runs an attendance loop every `capture.interval_seconds` (default 300,
floor 30) for as long as it is in the meeting. The browser page maintains a
presence ledger keyed by Zoom user id, fed by Zoom's own `user-added`,
`user-removed` and `user-updated` events, so a participant who joins and leaves
between two ticks is still recorded.

Each tick reports one cumulative row per participant:

```
POST {BACKEND}/api/tutor/bot/attendance
Header: X-Tutor-Bot-Secret: <secret>   (if configured)
```

```json
{
  "session_ref": "42",
  "runtime_id": "bot_01HXYZ…",
  "participant_id": "16778240",
  "participant_name": "Maria Gomez",
  "registrant_id": null,
  "observed_at": 1749500000.0,
  "joined_at": 1749496400.0,
  "left_at": null,
  "present": true,
  "video_on": true,
  "video_on_seconds": 3300,
  "observed_seconds": 3550,
  "face_present": true,
  "face_checked": true
}
```

These are **cumulative totals, not deltas**. The backend upserts on
`(session_id, participant_id)`, so the latest report replaces the durations for
that student. `face_checked` distinguishes "we looked and saw no face" from "we
never had a frame to look at", which matters because per-user frames are
unavailable on the Component View SDK (see `bot/README.md`).

Read it back with `GET /api/tutor/attendance?session_id=…`, which also returns a
summary of participants tracked, present now, on camera now, and total camera
seconds.

## Screenshots / per-student capture

If `capture.enabled` is true **and** a frame is actually obtainable, the bot
also files a screenshot manifest row:

1. Determines `video_on` (is the user's camera sending video?) and `face_present`
   (OpenCV face check on the frame). These cross-check each other: camera off ⇒
   `video_on:false`; camera on but no face ⇒ `face_present:false`.
2. If `capture.store_images` is true, uploads the frame to Google Drive
   (per-session folder) and keeps the link. If false, it discards the pixels and
   records only the presence flags.
3. Reports one manifest row to the backend:

```
POST {BACKEND}/api/tutor/bot/screenshots
Header: X-Tutor-Bot-Secret: <secret>   (if configured)
```

```json
{
  "session_ref": "42",
  "runtime_id": "bot_01HXYZ…",
  "participant_id": "p_55",
  "participant_name": "Maria Gomez",
  "registrant_id": null,            // populate once registration links are used
  "captured_at": 1749500000.0,
  "video_on": true,
  "face_present": true,
  "stored": true,
  "image_url": "https://drive.google.com/file/d/…/view",
  "drive_file_id": "1AbC…"
}
```

Attribution is by Zoom's stream identity (`participant_id`) plus `participant_name`
— never by tile position. Both are stored so either can serve as a failsafe for
the other.

## Notes

- **Meeting SDK credentials** (SDK Key/Secret, JWT signing) live entirely in your
  bot. The backend's existing Zoom Server-to-Server OAuth credentials are
  unrelated and are **not** sufficient to send in-meeting chat.
- A backend restart does **not** evict your bot from a meeting (separate
  process). `runtime_id` stays valid; pending approvals remain sendable.
- DMs require a `participant_id` your bot understands. Include it on inbound DM
  `chat` events so replies can target the right person.
