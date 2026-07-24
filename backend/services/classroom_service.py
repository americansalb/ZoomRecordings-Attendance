"""
Google Classroom posting.

Classroom stores no video. A post is a "course work material" whose
materials[] points at a file that already lives in Drive:

    materials: [{ driveFile: { driveFile: {id}, shareMode: "VIEW" } }]

So Drive upload stays a required first step and this is one call after it.

Auth: Classroom acts on behalf of a person, so a bare service account is not a
member of any course. We use domain-wide delegation to impersonate a teacher
(see docs/classroom-setup.md). If delegation isn't set up, every method here
degrades to a clear "not configured" result rather than raising — the publish
pipeline still uploads to Drive and hands back a link to post by hand.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Only what the impersonated teacher actually needs. Drive is deliberately NOT
# here: uploads run as the service account itself (see drive_service, which
# never calls with_subject), and attaching an existing file to a course needs
# no Drive scope on the acting user's token. Asking for it would mean one more
# scope for an admin to authorise, for nothing.
SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.topics",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials",
]


@dataclass
class ClassroomResult:
    """Outcome of a post attempt. `ok=False` is normal, not exceptional."""

    ok: bool
    material_id: Optional[str] = None
    link: Optional[str] = None
    state: Optional[str] = None
    reason: Optional[str] = None      # short machine-ish code
    detail: Optional[str] = None      # what a human should do about it

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "material_id": self.material_id,
            "link": self.link,
            "state": self.state,
            "reason": self.reason,
            "detail": self.detail,
        }


class ClassroomNotConfigured(Exception):
    """Raised internally when no teacher is configured; callers get a result."""


class ClassroomService:
    def __init__(self) -> None:
        self._clients: Dict[str, Any] = {}

    # -- plumbing ---------------------------------------------------------

    def _credentials(self, subject: str):
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
                raise ClassroomNotConfigured("No Google service account credentials found")
            creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)

        # Act as a real teacher rather than as the service account.
        return creds.with_subject(subject)

    def _client(self, subject: str):
        if not subject:
            raise ClassroomNotConfigured(
                "No Classroom teacher configured. Set one in Class settings."
            )
        if subject not in self._clients:
            self._clients[subject] = build(
                "classroom", "v1", credentials=self._credentials(subject), cache_discovery=False
            )
        return self._clients[subject]

    # Delegation failures surface as an OAuth RefreshError during token
    # exchange, not as an HttpError from the API call — so they need catching
    # separately or the raw Google string reaches the user.
    DELEGATION_HELP = (
        "Google hasn't authorised this app to post as a teacher yet. A Workspace admin "
        "needs to open Admin console → Security → Access and data control → API controls "
        "→ Domain-wide delegation, add the service account's numeric client ID, and grant "
        "these scopes: classroom.courses.readonly, classroom.topics, "
        "classroom.courseworkmaterials. Full steps are in docs/classroom-setup.md. "
        "Until then everything still uploads to Drive and gives you a link to post by hand."
    )

    @staticmethod
    def _explain_auth(e: Exception) -> ClassroomResult:
        text = str(e)
        if "unauthorized_client" in text:
            return ClassroomResult(
                ok=False,
                reason="delegation_not_authorized",
                detail=ClassroomService.DELEGATION_HELP,
            )
        if "invalid_grant" in text or "Invalid email" in text:
            return ClassroomResult(
                ok=False,
                reason="bad_subject",
                detail=(
                    "Google rejected that teacher email. Check it's a real account on your "
                    "Workspace domain, spelled exactly as it appears in the admin console."
                ),
            )
        logger.error(f"[CLASSROOM] Auth error: {text[:400]}")
        return ClassroomResult(
            ok=False,
            reason="auth_error",
            detail=f"Google refused the sign-in: {text[:200]}",
        )

    @staticmethod
    def _explain(e: HttpError) -> ClassroomResult:
        """Turn the documented failures into something actionable."""
        status = getattr(e.resp, "status", None)
        body = e.content.decode("utf-8", "replace") if isinstance(e.content, bytes) else str(e.content)

        if "unauthorized_client" in body or status == 401:
            return ClassroomResult(
                ok=False,
                reason="delegation_not_authorized",
                detail=(
                    "Google hasn't authorized this app to post as a teacher yet. A Workspace "
                    "admin needs to add the service account's client ID under Domain-wide "
                    "delegation (see docs/classroom-setup.md)."
                ),
            )
        # Checked before the status branches: Google returns this one as
        # FAILED_PRECONDITION (400), not 403, so testing it inside the 403 block
        # meant it never matched.
        if "AttachmentNotVisible" in body:
            return ClassroomResult(
                ok=False,
                reason="attachment_not_visible",
                detail=(
                    "Classroom can't see the uploaded video. The teacher account needs "
                    "access to the file — check the shared drive is one they belong to."
                ),
            )
        if status == 403:
            # Google's own message on a 403 is genuinely useful — for a disabled
            # API it contains a direct "enable it here" link with the project ID
            # already filled in. Throwing it away and printing a generic guess
            # makes this harder to fix, not easier.
            google_message = ""
            try:
                google_message = (json.loads(body).get("error") or {}).get("message", "")
            except (json.JSONDecodeError, AttributeError):
                pass

            if "has not been used in project" in google_message or "is disabled" in google_message:
                return ClassroomResult(
                    ok=False,
                    reason="api_not_enabled",
                    detail=(
                        "The Google Classroom API isn't switched on for your Google Cloud "
                        "project yet. Google says: " + google_message
                    ),
                )
            # Reading courses can succeed while writing fails, and the two have
            # different fixes — so don't lump them together.
            if (
                "insufficient authentication scopes" in google_message.lower()
                or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body
            ):
                return ClassroomResult(
                    ok=False,
                    reason="scope_missing",
                    detail=(
                        "Reading courses works, but posting doesn't — the "
                        "classroom.courseworkmaterials scope is missing from the domain-wide "
                        "delegation entry. In the Admin console, open the delegation row, click "
                        "Edit, and make sure all three of these are listed: "
                        "classroom.courses.readonly, classroom.topics, "
                        "classroom.courseworkmaterials."
                    ),
                )
            if "Requested entity was not found" in google_message:
                return ClassroomResult(
                    ok=False,
                    reason="not_a_classroom_user",
                    detail=(
                        "That account isn't set up in Google Classroom. Sign in to "
                        "classroom.google.com as it once to activate it, and make sure it's a "
                        "teacher on the courses you want to post to."
                    ),
                )
            # Google's message on this one is just "The caller does not have
            # permission", so the body is no help. The full response goes to the
            # log; the user gets the explanation that actually fits.
            #
            # Being a teacher is already established — the course picker lists
            # only courses where courses.list(teacherId="me") returned it. What
            # remains, and what Google documents for this call, is that it could
            # not "share a Drive attachment": Classroom posts as the teacher, and
            # the teacher has no rights over a file the service account owns.
            logger.error(f"[CLASSROOM] 403 on post: {body[:800]}")
            return ClassroomResult(
                ok=False,
                reason="permission_denied",
                detail=(
                    "Google refused the request"
                    + (f": {google_message}." if google_message else ".")
                    + " The most likely reason is that Classroom couldn't share the video "
                    "file: it posts as the teacher, and the teacher needs access to a file "
                    "the service account owns. Giving that account edit access to the Drive "
                    "folder fixes it."
                ),
            )
        if status == 404:
            return ClassroomResult(
                ok=False,
                reason="course_not_found",
                detail="That Classroom course no longer exists. Pick it again in Class settings.",
            )

        logger.error(f"[CLASSROOM] Unexpected error {status}: {body[:500]}")
        return ClassroomResult(
            ok=False,
            reason="error",
            detail=f"Classroom returned an unexpected error ({status}).",
        )

    # -- reads ------------------------------------------------------------

    def list_courses(self, subject: str) -> Dict[str, Any]:
        """Active courses the impersonated teacher teaches."""
        try:
            client = self._client(subject)
            resp = client.courses().list(
                teacherId="me", courseStates=["ACTIVE"], pageSize=100
            ).execute()
            courses = [
                {
                    "id": c["id"],
                    "name": c.get("name", ""),
                    "section": c.get("section", ""),
                    "link": c.get("alternateLink", ""),
                }
                for c in resp.get("courses", [])
            ]
            return {"ok": True, "courses": courses}
        except ClassroomNotConfigured as e:
            return {"ok": False, "courses": [], "reason": "not_configured", "detail": str(e)}
        except (RefreshError, GoogleAuthError) as e:
            r = self._explain_auth(e)
            return {"ok": False, "courses": [], "reason": r.reason, "detail": r.detail}
        except HttpError as e:
            r = self._explain(e)
            return {"ok": False, "courses": [], "reason": r.reason, "detail": r.detail}
        except Exception as e:                      # noqa: BLE001 - never 500 the settings page
            logger.error(f"[CLASSROOM] list_courses failed: {e}", exc_info=True)
            return {"ok": False, "courses": [], "reason": "error", "detail": str(e)}

    def list_topics(self, subject: str, course_id: str) -> Dict[str, Any]:
        try:
            client = self._client(subject)
            resp = client.courses().topics().list(courseId=course_id, pageSize=100).execute()
            topics = [{"id": t["topicId"], "name": t.get("name", "")} for t in resp.get("topic", [])]
            return {"ok": True, "topics": topics}
        except ClassroomNotConfigured as e:
            return {"ok": False, "topics": [], "reason": "not_configured", "detail": str(e)}
        except (RefreshError, GoogleAuthError) as e:
            r = self._explain_auth(e)
            return {"ok": False, "topics": [], "reason": r.reason, "detail": r.detail}
        except HttpError as e:
            r = self._explain(e)
            return {"ok": False, "topics": [], "reason": r.reason, "detail": r.detail}
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CLASSROOM] list_topics failed: {e}", exc_info=True)
            return {"ok": False, "topics": [], "reason": "error", "detail": str(e)}

    # -- the write we actually care about ---------------------------------

    def post_material(
        self,
        subject: str,
        course_id: str,
        drive_file_ids: List[str],
        title: str,
        description: str = "",
        topic_id: Optional[str] = None,
        state: str = "PUBLISHED",
        share_mode: str = "VIEW",
        scheduled_time: Optional[str] = None,
        drive_links: Optional[List[str]] = None,
    ) -> ClassroomResult:
        """
        Attach one or more already-uploaded Drive files to a course.

        Returns a ClassroomResult; never raises. A failure here must not lose
        the upload that already succeeded.

        If Google refuses because it can't share the attachment, this falls back
        to posting the same material with the Drive links written into the
        description. Students still get to the video, and the result says plainly
        which of the two happened.
        """
        if not course_id:
            return ClassroomResult(
                ok=False,
                reason="no_course",
                detail="This class has no Classroom course selected in Class settings.",
            )
        if not drive_file_ids:
            return ClassroomResult(ok=False, reason="no_files", detail="Nothing to attach.")

        body: Dict[str, Any] = {
            "title": title,
            "materials": [
                {"driveFile": {"driveFile": {"id": fid}, "shareMode": share_mode}}
                for fid in drive_file_ids
            ],
            "state": state if state in ("PUBLISHED", "DRAFT") else "PUBLISHED",
        }
        if description:
            body["description"] = description
        if topic_id:
            body["topicId"] = topic_id
        if scheduled_time:
            # Classroom requires DRAFT for a scheduled post.
            body["state"] = "DRAFT"
            body["scheduledTime"] = scheduled_time

        try:
            client = self._client(subject)
            created = client.courses().courseWorkMaterials().create(
                courseId=course_id, body=body
            ).execute()
            logger.info(f"[CLASSROOM] Posted material {created.get('id')} to course {course_id}")
            return ClassroomResult(
                ok=True,
                material_id=created.get("id"),
                link=created.get("alternateLink"),
                state=created.get("state"),
            )
        except ClassroomNotConfigured as e:
            return ClassroomResult(ok=False, reason="not_configured", detail=str(e))
        except (RefreshError, GoogleAuthError) as e:
            return self._explain_auth(e)
        except HttpError as e:
            failure = self._explain(e)
            if failure.reason in ("permission_denied", "attachment_not_visible") and drive_links:
                recovered = self._post_link_only(
                    subject=subject,
                    course_id=course_id,
                    title=title,
                    description=description,
                    topic_id=topic_id,
                    state=body["state"],
                    scheduled_time=scheduled_time,
                    drive_links=drive_links,
                )
                if recovered:
                    return recovered
            return failure
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CLASSROOM] post_material failed: {e}", exc_info=True)
            return ClassroomResult(ok=False, reason="error", detail=str(e))

    def _post_link_only(
        self,
        subject: str,
        course_id: str,
        title: str,
        description: str,
        topic_id: Optional[str],
        state: str,
        scheduled_time: Optional[str],
        drive_links: List[str],
    ) -> Optional[ClassroomResult]:
        """
        Post the material with the video as a link in the text instead of an
        attached file.

        Attaching makes Classroom re-share the file as the teacher, which is the
        step that gets refused. A plain link asks nothing of the teacher's
        permissions, so this goes through when the attachment doesn't — and the
        class still gets the recording tonight.

        Returns None if this fails too, so the caller reports the original error
        rather than a second, more confusing one.
        """
        lines = [description] if description else []
        lines.append("Recording: " + "  ".join(drive_links))
        body: Dict[str, Any] = {
            "title": title,
            "description": "\n\n".join(lines),
            "state": state,
        }
        if topic_id:
            body["topicId"] = topic_id
        if scheduled_time:
            body["scheduledTime"] = scheduled_time

        try:
            created = self._client(subject).courses().courseWorkMaterials().create(
                courseId=course_id, body=body
            ).execute()
        except Exception as e:                      # noqa: BLE001
            logger.warning(f"[CLASSROOM] Link-only fallback also failed: {e}")
            return None

        logger.info(
            f"[CLASSROOM] Posted {created.get('id')} to {course_id} as a link "
            f"(attachment was refused)"
        )
        return ClassroomResult(
            ok=True,
            material_id=created.get("id"),
            link=created.get("alternateLink"),
            state=created.get("state"),
            reason="posted_as_link",
            detail=(
                "Posted, but with the video as a link rather than an attached file — "
                "Google wouldn't let the teacher account share the file itself. "
                "Students can still open it."
            ),
        )

    def is_configured(self, subject: str) -> bool:
        return bool(subject)


classroom_service = ClassroomService()
