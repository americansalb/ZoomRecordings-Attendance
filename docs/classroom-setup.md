# Posting recordings to Google Classroom

Notes for wiring the trim-and-upload pipeline through to Classroom, and how to
run the feasibility spike before building anything real.

## The thing to understand first

**Classroom does not store video.** There is no upload endpoint. What you create
is a *course work material* on a course, whose `materials[]` array points at a
file that already lives in Google Drive:

```jsonc
POST /v1/courses/{courseId}/courseWorkMaterials
{
  "title": "Session 127 (Night) — Day 5 (Nov 19)",
  "description": "Recording of Day 5.",
  "topicId": "...",                       // optional
  "state": "PUBLISHED",                   // or DRAFT
  "scheduledTime": "2025-11-21T14:00:00Z",// optional, with state DRAFT
  "materials": [
    { "driveFile": { "driveFile": { "id": "<drive file id>" },
                     "shareMode": "VIEW" } }
  ]
}
```

So the existing Drive upload is not a workaround to be replaced — it is a
required first step. Classroom is one extra API call after it.

`shareMode` may be `VIEW`, `EDIT`, or `STUDENT_COPY`; the latter two are only
valid on assignment-type coursework, so recordings use `VIEW`.

## Auth: domain-wide delegation

The Classroom API acts on behalf of a **person**. A bare service account is not
a member of any course, so the credentials in `services/drive_service.py` are
not sufficient on their own — they need `.with_subject("teacher@aalb.org")` so
the service account impersonates a teacher.

That impersonation must be authorized once by a Workspace super admin:

1. **Admin console** → Security → Access and data control → API controls
2. **Domain-wide delegation** → *Manage domain-wide delegation* → *Add new*
3. **Client ID**: the service account's numeric *Unique ID* — not its email
   address. Find it in Google Cloud Console → IAM & Admin → Service Accounts.
4. **OAuth scopes**, comma separated, matching character for character:

```
https://www.googleapis.com/auth/drive,
https://www.googleapis.com/auth/classroom.courses.readonly,
https://www.googleapis.com/auth/classroom.topics,
https://www.googleapis.com/auth/classroom.courseworkmaterials
```

Also enable the **Google Classroom API** on the Cloud project.

Changes can take a few minutes to propagate. The impersonated account must be a
**teacher** on every course you post to — students and non-members are rejected.

Google's own docs recommend avoiding domain-wide delegation where possible. The
alternative is a one-time OAuth consent by a teacher plus a stored refresh
token; that avoids the admin step but adds token storage and breaks when that
person leaves. Delegation is the better fit for an unattended backend.

## Finding the course — decided once, not per recording

`courses.list(teacherId="me", courseStates=["ACTIVE"])` returns the real courses
with their IDs. The Class settings screen populates its course dropdown from
that call, and the chosen `courseId` is stored against the class.

After that, nothing is searched or guessed at publish time: a recording titled
`Session 127. …` resolves to class `127`, which already holds its `courseId` and
`topicId`. This replaces the current heuristic that text-scans an unrelated
schedule spreadsheet (`drive_service.get_day_number`) and falls back to "the
first sheet", then to day `0`.

## The risk worth testing first

Classroom shares the attachment **on behalf of the acting teacher**. The service
account uploads the file, so the service account owns it — and if the teacher
cannot see that file, the API fails with:

```
FAILED_PRECONDITION — AttachmentNotVisible
```

Two ways around it:

1. Upload into a folder inside a **Shared Drive** the teachers belong to, so the
   file is owned by the shared drive rather than by the service account. The app
   already uploads to a shared folder, so this may already hold — but "shared
   folder" and "Shared Drive" are not the same thing, which is exactly why this
   needs testing rather than assuming.
2. Or grant the teacher explicit reader access on the file immediately after
   upload, before creating the material.

Worth deciding separately: today `drive_service._set_file_permissions` grants
`anyone with the link` reader access. Once Classroom mediates access, an open
link may no longer be wanted.

## Running the spike

`backend/scripts/classroom_spike.py` answers all of the above against the real
domain. It is standalone — nothing imports it.

```bash
cd backend
export GOOGLE_CLIENT_EMAIL=... GOOGLE_PRIVATE_KEY=...   # or GOOGLE_SERVICE_ACCOUNT_FILE

# 1. Does delegation work, and what does this teacher teach?
python scripts/classroom_spike.py --subject teacher@aalb.org --list-courses

# 2. Full round trip: generate a 10s clip, upload, attach as material
python scripts/classroom_spike.py \
    --subject teacher@aalb.org \
    --course-id 987654321 \
    --folder-id <drive folder id> \
    --topic "Class recordings"

# 3. Publish for real to check the student view, then remove everything
python scripts/classroom_spike.py ... --publish --cleanup
```

Materials are created as **DRAFT** unless `--publish` is passed, so running this
against a live course does not notify students.

Each documented failure is translated into the fix rather than a raw stack
trace: rejected delegation prints the exact scope list for the admin console,
`AttachmentNotVisible` prints the Shared Drive remedy, `403` lists the three
usual causes.

## What to record from the run

- Does delegation work? (if not, what the admin still needs to do)
- Which account will the backend impersonate in production?
- Is the upload target a real Shared Drive, or a folder in someone's My Drive?
- Did the attach succeed, and can a *student* account actually play the video?

Those four answers are what the real `classroom_service.py` gets built on.
