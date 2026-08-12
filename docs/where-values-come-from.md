# Where every value comes from

One row per thing the publish flow decides, in the order it's decided. Read it
as: **first source wins; if that's blank, fall to the next; you can always
override.** Nothing is guessed silently — where a value can't be determined, the
UI asks instead of inventing one.

## The chain, in order

| # | Value | Primary source | Falls back to | You override it |
| - | ----- | -------------- | ------------- | --------------- |
| 1 | **Session number** | Zoom recording title, regex `Session\s*(\d{3})` | — (recording is "not matched") | Class dropdown, review screen |
| 2 | **Class settings** | `publish_classes.json`, keyed by session number | — (recording is "not configured") | Class settings screen |
| 3 | **Time zone** | The class's `timezone` | Account-level default time zone | Both are dropdowns in settings |
| 4 | **Recording date** | Zoom `start_time` (UTC) converted to #3 | — | Not editable — it's a fact |
| 5 | **Day number** | Counted from the class's `first_class_date` + `meeting_weekdays` | — (blank, and you're asked) | Day field, review screen |
| 6 | **Trim window** | Hand-typed start time, if given | Class `scheduled_start`/`end` ± padding | Timeline handles, timecode fields |
| 7 | **Which videos** | Class `views` list | Speaker + screenshare alone | Checkboxes, review screen |
| 8 | **Filename** | Class `filename_pattern` with tokens filled | Zoom title + date, for unmatched | Pattern in class settings |
| 9 | **Title students see** | Class `title_pattern` | Zoom title | Title field, review screen |
| 10 | **Drive folder** | `Session {code}` / view folder | `Unsorted` / view folder | — |
| 11 | **Classroom course** | Class `classroom_course_id` | — (Drive-only upload) | Course dropdown in settings |
| 12 | **Classroom topic** | Class `classroom_topic_id` | — (posts with no topic) | Topic dropdown in settings |
| 13 | **Posting account** | Account setting `classroom_subject` | — (Drive-only upload) | Teacher email in settings |

## Detail on the ones that bite

### 1. Session number — from the Zoom title

`services/publish_planner.py :: extract_session_code`

```
"Session 139 Schedule (Accelerated): Mondays, Wednesdays, and Fridays…"
         ^^^  three digits, case-insensitive
```

Two digits won't match — `Session 99` is ignored deliberately, so a stray number
in a title can't be mistaken for a session.

### 3–4. Time zone, then date

The recording's UTC timestamp is converted to the class's local time **before**
the date is taken. This is why the time zone matters more than it looks: an 11pm
Nov 19 class is `03:55 UTC on Nov 20`, and using UTC would file it under the
wrong day, in the filename and the title both.

### 5. Day number — counted, not looked up

`services/class_config.py :: ClassSettings.day_number_for`

Walks forward from `first_class_date`, counting only dates that fall on
`meeting_weekdays`. A date that isn't a meeting day returns nothing, and the
review screen asks you for it rather than shipping "Day 0" — which is what the
old flow did when its spreadsheet lookup failed.

### 6. Trim window — three ways, in priority order

`services/publish_planner.py :: compute_trim`

1. **A start time you typed** — keeps 5 min before it and 10 min after the class
   length you picked (3 hours by default)
2. **The class schedule** — `scheduled_start` to `scheduled_end`, plus each
   class's `pad_before_minutes` / `pad_after_minutes` (5 before, 10 after by
   default — classes run over more often than they start early)
3. **Neither** — the whole recording, and the screen asks when class started

Two guards, both tested: a recording whose start is more than 4 hours before or
30 minutes after the scheduled time is treated as not matching the schedule
(whole recording kept), and the end is always clamped to the real video length.

### 7. Which videos — from Zoom's own recording types

`services/class_config.py :: VIEW_TYPES` maps Zoom's `recording_type` values to
plain names. Zoom's naming is misleading, so this table is the translation:

| Zoom `recording_type` | What it actually is |
| --- | --- |
| `shared_screen_with_speaker_view` | Shared screen + active speaker — the usual one |
| `shared_screen_with_gallery_view` | Shared screen + gallery of faces (**does** include the screen) |
| `gallery_view` | Gallery of faces only — no shared screen |
| `shared_screen` | Shared screen only — no cameras |
| `active_speaker` | Active speaker only — no shared screen |
| `audio_only` | Sound, no picture |

Only the types Zoom actually produced for that meeting are offered.

**Zoom does not always spell these the way its own table does.** A recording made
with closed captions comes back as `shared_screen_with_speaker_view(CC)` — the
same screen-plus-speaker video under a decorated name. Matching the raw string
meant that recording appeared to have no screen share at all, and the review
screen offered only the gallery files. So `services/publish_planner.py ::
collect_views` matches on the type with the decoration and casing stripped
(`services/class_config.py :: normalize_zoom_type`), and follows three more rules:

| Situation | What happens |
| --- | --- |
| A video type we have no name for | Offered anyway, named from Zoom's own type — never dropped |
| A transcript, chat log or timeline | Never offered; it isn't a recording of the class |
| Several files of one type (recording stopped and restarted) | The longest one is sent, and the screen says how many there were |
| The class asked for a view Zoom didn't record | The next best is preselected **and** the screen names what's missing |

That last one is a Zoom-side setting, not something this tool can fix after the
fact: the layouts Zoom records are chosen in the hosting account's cloud
recording settings, before the class runs.

### 8–9. Filename and title

Tokens available in both patterns: `{session}` `{day}` `{date}` `{view}`
`{course}`.

The title has one hard guarantee, enforced after the pattern runs: **the
recording date is always in it**. If the pattern omits it, it's appended. And
`Day N` only appears when N is actually known — never `Day None`.

### 11–13. Classroom

The course is chosen once per class from the real list Google returns
(`courses.list`), and stored as an ID. Nothing is matched by name at publish
time. If no course is set, the video still uploads to Drive and you get the link
to attach by hand.

## What gets read from the Classroom course name

Picking a course in Class settings prefills the form from its name, because your
course names already state everything:

```
AALB Medical Interpreter Training (Session 139 Schedule (Accelerated):
Mondays, Wednesdays, and Fridays (Night | July 17th to August 8th, 2026))
        │                          │                     │
        │                          │                     └── term start -> first class date
        │                          └── meeting weekdays
        └── session number
```

`services/course_parser.py` extracts:

| Field | From |
| --- | --- |
| Session number | `Session 139` |
| Meeting weekdays | `Mondays, Wednesdays, and Fridays` |
| Term start / end | `July 17th to August 8th, 2026` |
| First class date | First meeting weekday on or after the term start |
| Part of day | `Night` — a label only |

**Class start and end times are never inferred.** "Night" is in the title; 11pm
is not. Guessing would produce a wrong trim on every recording of that class, so
those two fields stay empty until you set them.

Prefill only ever fills **blank** fields — it won't overwrite something you
typed.

## Still entered by hand, and why

| Field | Why it can't be derived |
| --- | --- |
| Class start / end time | Not stated anywhere in Zoom or Classroom data |
| Teacher email to post as | A choice about identity, not a fact to look up |
| Trim padding | Policy, not data (defaults to 5 min before, 10 after) |
| Filename / title patterns | Naming convention |
