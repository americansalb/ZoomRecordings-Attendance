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
        if "AttachmentNotVisible" in body:
            return ClassroomResult(
                ok=False,
                reason="attachment_not_visible",
                detail=(
                    "Classroom can't see the uploaded video. It needs to live in a Shared "
                    "Drive the teacher belongs to, rather than being owned by the service "
                    "account alone."
                ),
            )
        if status == 403:
            return ClassroomResult(
                ok=False,
                reason="permission_denied",
                detail=(
                    "Permission denied. Either the Classroom API isn't enabled, the teacher "
                    "isn't a teacher on this course, or the course is archived."
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
    ) -> ClassroomResult:
        """
        Attach one or more already-uploaded Drive files to a course.

        Returns a ClassroomResult; never raises. A failure here must not lose
        the upload that already succeeded.
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
            return self._explain(e)
        except Exception as e:                      # noqa: BLE001
            logger.error(f"[CLASSROOM] post_material failed: {e}", exc_info=True)
            return ClassroomResult(ok=False, reason="error", detail=str(e))

    def is_configured(self, subject: str) -> bool:
        return bool(subject)


classroom_service = ClassroomService()
