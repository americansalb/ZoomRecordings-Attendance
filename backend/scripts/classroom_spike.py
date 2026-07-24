#!/usr/bin/env python3
"""
Google Classroom feasibility spike — throwaway, not wired into the app.

Answers the questions we can't answer from the docs, because they depend on
how this Workspace domain is configured:

  1. Does domain-wide delegation actually work for our service account?
  2. Can the impersonated teacher see the courses we expect?
  3. Can Classroom attach a Drive file that the SERVICE ACCOUNT uploaded?
     (This is the risky one. Classroom shares the attachment on behalf of the
     acting teacher; if that teacher can't see the file, the API returns
     FAILED_PRECONDITION / AttachmentNotVisible.)

Nothing here is imported by the backend. Delete the file once the answers are
recorded, or promote the pieces that worked into services/classroom_service.py.

Usage
-----
  # 1. What courses does this teacher have?
  python backend/scripts/classroom_spike.py --subject teacher@aalb.org --list-courses

  # 2. Full round trip against one course (posts a DRAFT by default)
  python backend/scripts/classroom_spike.py \
      --subject teacher@aalb.org \
      --course-id 987654321 \
      --folder-id 1k00chNZpP7rLOZvLE3rrMjIK_scVthaw

  # 3. Same, but actually publish to students, then delete everything after
  python backend/scripts/classroom_spike.py ... --publish --cleanup

Safety
------
Materials are created as DRAFT unless you pass --publish. A draft is visible to
teachers only, so running this against a live course does not notify students.

Credentials come from the same place the app already looks:
GOOGLE_CLIENT_EMAIL + GOOGLE_PRIVATE_KEY, or GOOGLE_SERVICE_ACCOUNT_FILE.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Drive is needed to upload the clip; Classroom to read courses/topics and post.
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.topics",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials",
]

OK = "  ok  "
FAIL = " FAIL "
INFO = "  ..  "


def say(mark: str, msg: str) -> None:
    print(f"[{mark}] {msg}", flush=True)


def die(msg: str, hint: str = "") -> None:
    say(FAIL, msg)
    if hint:
        print("\n" + hint.strip() + "\n", file=sys.stderr)
    sys.exit(1)


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def credentials(subject: str):
    """Service-account credentials impersonating `subject` via delegation."""
    client_email = os.getenv("GOOGLE_CLIENT_EMAIL")
    private_key = os.getenv("GOOGLE_PRIVATE_KEY")

    if client_email and private_key:
        info = {
            "type": "service_account",
            "client_email": client_email,
            "private_key": private_key.replace("\\n", "\n"),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
        if not os.path.exists(path):
            die(
                f"No credentials: set GOOGLE_CLIENT_EMAIL/GOOGLE_PRIVATE_KEY or put a "
                f"service account JSON at {path}"
            )
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)

    # This line is the whole point: act as a real teacher, not as the robot.
    return creds.with_subject(subject)


def explain(e: HttpError, context: str) -> None:
    """Translate the handful of failures this spike is designed to provoke."""
    status = getattr(e.resp, "status", None)
    body = e.content.decode("utf-8", "replace") if isinstance(e.content, bytes) else str(e.content)

    if "unauthorized_client" in body or status == 401:
        die(
            f"{context}: delegation was rejected.",
            """
The Workspace admin has not authorized this service account for these scopes.
Admin console -> Security -> Access and data control -> API controls ->
Domain-wide delegation -> Add new. Use the service account's *client ID*
(the numeric "Unique ID" on the service account, not its email) and paste
this exact scope list, comma separated:

  https://www.googleapis.com/auth/drive,
  https://www.googleapis.com/auth/classroom.courses.readonly,
  https://www.googleapis.com/auth/classroom.topics,
  https://www.googleapis.com/auth/classroom.courseworkmaterials

Scopes must match character for character. Changes can take a few minutes.
            """,
        )

    if "AttachmentNotVisible" in body:
        die(
            f"{context}: Classroom cannot see the Drive file.",
            """
This is the ownership problem. The service account uploaded the file, so it
owns it, and the impersonated teacher has no access — Classroom refuses to
attach a file it cannot share.

Fixes, cheapest first:
  1. Upload into a SHARED DRIVE the teachers are members of, so the file is
     owned by the shared drive rather than the service account. Pass that
     folder as --folder-id.
  2. Or grant the teacher explicit reader access on the file right after
     upload, before creating the material.
            """,
        )

    if status == 403:
        die(
            f"{context}: permission denied.",
            """
Usual causes:
  - The Classroom API is not enabled on the Cloud project.
  - The impersonated user is not a TEACHER on this course (students and
    non-members cannot post material).
  - The course is archived.
            """,
        )

    die(f"{context}: {status} {body[:600]}")


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def list_courses(classroom) -> List[Dict[str, Any]]:
    try:
        resp = classroom.courses().list(
            teacherId="me", courseStates=["ACTIVE"], pageSize=100
        ).execute()
    except HttpError as e:
        explain(e, "Listing courses")
    return resp.get("courses", [])


def list_topics(classroom, course_id: str) -> List[Dict[str, Any]]:
    try:
        resp = classroom.courses().topics().list(courseId=course_id, pageSize=100).execute()
    except HttpError as e:
        explain(e, "Listing topics")
    return resp.get("topic", [])


def make_test_clip(path: str) -> None:
    """10 seconds of colour bars + a tone, so we upload something real."""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except FileNotFoundError:
        die("ffmpeg not found. Install it, or pass --file with your own mp4.")
    except subprocess.CalledProcessError as e:
        die(f"ffmpeg failed: {e.stderr.decode('utf-8', 'replace')[:400]}")


def upload(drive, path: str, folder_id: str, name: str) -> Dict[str, Any]:
    media = MediaFileUpload(path, mimetype="video/mp4", resumable=False)
    try:
        return drive.files().create(
            body={"name": name, "parents": [folder_id]},
            media_body=media,
            fields="id, name, webViewLink, owners(emailAddress), driveId",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        explain(e, "Uploading to Drive")


def post_material(
    classroom,
    course_id: str,
    file_id: str,
    title: str,
    topic_id: Optional[str],
    publish: bool,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "title": title,
        "description": "Posted by the Classroom feasibility spike. Safe to delete.",
        "materials": [{
            "driveFile": {
                "driveFile": {"id": file_id},
                "shareMode": "VIEW",
            }
        }],
        "state": "PUBLISHED" if publish else "DRAFT",
    }
    if topic_id:
        body["topicId"] = topic_id

    try:
        return classroom.courses().courseWorkMaterials().create(
            courseId=course_id, body=body
        ).execute()
    except HttpError as e:
        explain(e, "Creating course work material")


# --------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Google Classroom feasibility spike")
    p.add_argument("--subject", required=True, help="Teacher to impersonate, e.g. teacher@aalb.org")
    p.add_argument("--list-courses", action="store_true", help="Just list courses and exit")
    p.add_argument("--course-id", help="Course to post the test material to")
    p.add_argument("--folder-id", default=os.getenv("CLASSROOM_SPIKE_FOLDER_ID"),
                   help="Drive folder for the upload (use a Shared Drive folder)")
    p.add_argument("--file", help="Use this mp4 instead of generating a 10s test clip")
    p.add_argument("--topic", help="Topic name to post under (created if missing)")
    p.add_argument("--publish", action="store_true",
                   help="Publish to students. Without this the material is a teacher-only DRAFT.")
    p.add_argument("--cleanup", action="store_true", help="Delete the material and file at the end")
    args = p.parse_args()

    say(INFO, f"Impersonating {args.subject}")
    creds = credentials(args.subject)
    classroom = build("classroom", "v1", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    # --- 1. delegation + course visibility -------------------------------
    courses = list_courses(classroom)
    say(OK, f"Delegation works. {args.subject} teaches {len(courses)} active course(s).")
    for c in courses:
        print(f"        {c['id']}  {c.get('name')}  ({c.get('section') or 'no section'})")

    if args.list_courses:
        print("\nPick one and re-run with --course-id <id> --folder-id <drive folder>.")
        return

    if not args.course_id:
        die("Need --course-id (run with --list-courses first).")
    if not args.folder_id:
        die("Need --folder-id (a Drive folder, ideally inside a Shared Drive).")

    if args.course_id not in {c["id"] for c in courses}:
        say(FAIL, f"{args.subject} is not a teacher on course {args.course_id}.")
        die("Classroom will refuse to post. Pick a course from the list above.")

    # --- 2. topics --------------------------------------------------------
    topics = list_topics(classroom, args.course_id)
    say(OK, f"Course has {len(topics)} topic(s): {[t.get('name') for t in topics] or 'none'}")

    topic_id = None
    if args.topic:
        match = next((t for t in topics if t.get("name") == args.topic), None)
        if match:
            topic_id = match["id"]
            say(OK, f"Using existing topic '{args.topic}' ({topic_id})")
        else:
            try:
                created = classroom.courses().topics().create(
                    courseId=args.course_id, body={"name": args.topic}
                ).execute()
                topic_id = created["id"]
                say(OK, f"Created topic '{args.topic}' ({topic_id})")
            except HttpError as e:
                explain(e, "Creating topic")

    # --- 3. upload --------------------------------------------------------
    tmp = None
    path = args.file
    if not path:
        tmp = tempfile.mkdtemp(prefix="classroom_spike_")
        path = os.path.join(tmp, "spike-clip.mp4")
        say(INFO, "Generating a 10s test clip with ffmpeg...")
        make_test_clip(path)

    size_mb = os.path.getsize(path) / 1024 / 1024
    say(INFO, f"Uploading {os.path.basename(path)} ({size_mb:.1f} MB) to folder {args.folder_id}")
    uploaded = upload(drive, path, args.folder_id, "Classroom spike — safe to delete.mp4")
    owners = [o.get("emailAddress") for o in uploaded.get("owners", [])]
    in_shared_drive = bool(uploaded.get("driveId"))
    say(OK, f"Uploaded {uploaded['id']}")
    print(f"        owner: {owners or 'shared drive (no individual owner)'}")
    print(f"        in a Shared Drive: {in_shared_drive}")
    if not in_shared_drive:
        say(INFO, "Not a Shared Drive — this is where AttachmentNotVisible usually appears.")

    # --- 4. the actual question -------------------------------------------
    state = "PUBLISHED" if args.publish else "DRAFT"
    say(INFO, f"Creating course work material as {state}...")
    material = post_material(
        classroom, args.course_id, uploaded["id"],
        "Classroom spike — safe to delete", topic_id, args.publish,
    )
    say(OK, f"Material created: {material['id']} ({material.get('state')})")
    print(f"        {material.get('alternateLink')}")

    print("\n" + "=" * 68)
    print("ANSWER: yes — Classroom accepted a service-account-uploaded Drive file.")
    print("=" * 68)
    print(json.dumps({
        "courseId": args.course_id,
        "materialId": material["id"],
        "driveFileId": uploaded["id"],
        "state": material.get("state"),
        "link": material.get("alternateLink"),
        "sharedDrive": in_shared_drive,
    }, indent=2))

    if args.publish:
        print("\nNOTE: this was PUBLISHED. Students can see it. Delete it when done.")

    # --- 5. cleanup -------------------------------------------------------
    if args.cleanup:
        try:
            classroom.courses().courseWorkMaterials().delete(
                courseId=args.course_id, id=material["id"]
            ).execute()
            say(OK, "Deleted the material")
        except HttpError as e:
            say(FAIL, f"Could not delete material: {e}")
        try:
            drive.files().delete(fileId=uploaded["id"], supportsAllDrives=True).execute()
            say(OK, "Deleted the Drive file")
        except HttpError as e:
            say(FAIL, f"Could not delete Drive file: {e}")
    else:
        print("\nLeft in place. Re-run with --cleanup to remove, or delete by hand.")

    if tmp:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
