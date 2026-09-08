# CIA recordings deliver themselves to the grading wizard

Exam recordings no longer need a hand upload to Google Drive. When a Zoom
cloud recording's title carries the word CIA, or the phrase "consecutive
interpreting assessment" (or "interpretation"), this service delivers it
into the grading wizard's Drive intake folder by itself. Class sessions
are untouched: they keep going through the publish screen as before.

## What one delivery looks like

One subfolder per exam appears in the wizard's intake folder, named from
the Zoom topic plus the Eastern date, for example
`CIA - Maria Lopez - Sep8/`. Inside it: the best video Zoom produced
(speaker view preferred) as `CIA - Maria Lopez - Sep8.mp4`, and the
audio-only copy beside it when Zoom made one. Staff then open the grading
wizard and pick it, same as a hand-uploaded exam.

Exam files are never link-shared. Class recordings get the
anyone-with-the-link permission so students can watch them; exam files
get no sharing at all, and only the accounts with access to the folder
can see them.

## How it stays correct

- Each delivered exam folder is stamped (in Drive itself) with the Zoom
  recording's ID. A recording already delivered is skipped forever, even
  after the wizard moves the folder to the graded area. No local state
  is trusted, so restarts and the free tier's naps change nothing.
- A recording Zoom is still processing is left alone and retried on the
  next sweep. A half-finished delivery (the upload failed partway) has
  no stamp yet, so the next sweep finishes it; uploads replace in place,
  so retries never create duplicates.

## How it is triggered

This API sleeps when idle on the free tier, so it cannot be its own alarm
clock. The attendance bot's computer, which is always awake, pokes
`POST /api/cia/sweep` every 45 minutes; the poke wakes the API and runs
one sweep. While the API happens to be awake it also sweeps hourly on its
own. Overlapping triggers are harmless: one sweep runs at a time.

The secret rule is the one every other bot endpoint follows: when
`TUTOR_BOT_SHARED_SECRET` (or `CIA_SWEEP_SECRET`) is set on the API the
poke must carry it; when neither is set the poke is accepted as it is.
(The first build refused every poke when no secret was set, and the live
API has none, so nothing was delivered until build capture-57.)

`GET /api/cia/status` shows what the last sweep did. With a secret
configured it shows everything, recording titles included; with none it
shows the counts and the error messages only, because titles name
candidates.

The bot's health page (`/healthz` on the bot's computer) carries a `cia`
block: whether the poke is running, when it last poked, what the API
answered, and the last sweep's summary as read three minutes after the
poke. That is where to look first when an exam has not turned up.

## Settings (all optional)

| Env var | Meaning | Default |
| --- | --- | --- |
| `CIA_DRIVE_FOLDER_ID` | The wizard's intake folder | The wizard's own default folder |
| `CIA_SWEEP_LOOKBACK_DAYS` | How far back each sweep looks | 7 |
| `CIA_SWEEP_SECRET` | A second trigger secret besides the bot's | unset |
| `CIA_SWEEP_DISABLED` | Set to anything to turn the whole feature off | unset |
| `BOT_CIA_SWEEP_PING_MINUTES` | On the bot: poke interval, 0 turns the poke off | 45 |

## If a sweep reports a permission error

The uploader's Google account needs access to the wizard's intake folder.
The error message in the logs, in `/api/cia/status`, and in the bot's
`/healthz` `cia` block names the exact account; share the intake folder
with it as Editor and the next sweep delivers everything it skipped.
