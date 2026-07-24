"""
Tests for posting a recording to Google Classroom.

The failure being protected against: Classroom posts *as the teacher*, and
attaching a Drive file makes it re-share that file on the teacher's behalf. The
service account owns these uploads, so Google refuses with a bare 403
"The caller does not have permission" — which says nothing about the real cause.

Two answers, both tested here: give the teacher access to the file before
posting, and if Google still refuses, post the link instead of the attachment so
the class gets the recording anyway.

Runs against a fake Classroom API, so no credentials and no network.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from googleapiclient.errors import HttpError  # noqa: E402

from services.classroom_service import ClassroomService  # noqa: E402


class FakeResponse:
    def __init__(self, status: int):
        self.status = status
        self.reason = "error"


def http_error(status: int, message: str, extra: str = "") -> HttpError:
    body = json.dumps({
        "error": {"code": status, "message": message, "status": extra or "PERMISSION_DENIED"}
    }).encode("utf-8")
    return HttpError(FakeResponse(status), body)


class FakeCreate:
    def __init__(self, owner: "FakeMaterials", body: Dict[str, Any]):
        self._owner = owner
        self._body = body

    def execute(self):
        self._owner.bodies.append(self._body)
        error = self._owner.error_for(self._body)
        if error:
            raise error
        return {
            "id": f"material-{len(self._owner.bodies)}",
            "alternateLink": "https://classroom.google.com/c/x/m/y",
            "state": self._body.get("state"),
        }


class FakeMaterials:
    """Refuses attachments if `refuse_attachments`, accepts plain posts."""

    def __init__(self, refuse_attachments: bool = False, refuse_everything: bool = False):
        self.bodies: List[Dict[str, Any]] = []
        self.refuse_attachments = refuse_attachments
        self.refuse_everything = refuse_everything

    def error_for(self, body) -> Optional[HttpError]:
        if self.refuse_everything:
            return http_error(403, "The caller does not have permission")
        if self.refuse_attachments and body.get("materials"):
            return http_error(403, "The caller does not have permission")
        return None

    def create(self, courseId=None, body=None):
        return FakeCreate(self, body)


class FakeCourses:
    def __init__(self, materials: FakeMaterials):
        self._materials = materials

    def courseWorkMaterials(self):
        return self._materials


class FakeClient:
    def __init__(self, materials: FakeMaterials):
        self._courses = FakeCourses(materials)

    def courses(self):
        return self._courses


def service_with(materials: FakeMaterials) -> ClassroomService:
    svc = ClassroomService()
    svc._clients["teacher@aalb.org"] = FakeClient(materials)
    return svc


def post(svc: ClassroomService, **overrides):
    args = dict(
        subject="teacher@aalb.org",
        course_id="course-1",
        drive_file_ids=["file-1"],
        title="Session 139 — Day 2 (Jul 22)",
        drive_links=["https://drive.google.com/file/d/file-1/view"],
    )
    args.update(overrides)
    return svc.post_material(**args)


class TestTheNormalPath(unittest.TestCase):
    def test_attaches_the_drive_file(self):
        materials = FakeMaterials()
        result = post(service_with(materials))
        self.assertTrue(result.ok)
        attached = materials.bodies[0]["materials"][0]["driveFile"]
        self.assertEqual(attached["driveFile"]["id"], "file-1")
        self.assertEqual(attached["shareMode"], "VIEW")

    def test_only_posts_once_when_it_works(self):
        materials = FakeMaterials()
        post(service_with(materials))
        self.assertEqual(len(materials.bodies), 1)

    def test_no_fallback_marker_on_a_clean_post(self):
        result = post(service_with(FakeMaterials()))
        self.assertIsNone(result.reason)


class TestWhenGoogleRefusesTheAttachment(unittest.TestCase):
    """The observed production failure."""

    def test_falls_back_to_posting_the_link(self):
        materials = FakeMaterials(refuse_attachments=True)
        result = post(service_with(materials))

        self.assertTrue(result.ok, "the class should still get the recording")
        self.assertEqual(result.reason, "posted_as_link")
        second = materials.bodies[1]
        self.assertNotIn("materials", second)
        self.assertIn("https://drive.google.com/file/d/file-1/view", second["description"])

    def test_says_plainly_that_it_is_a_link_not_an_attachment(self):
        result = post(service_with(FakeMaterials(refuse_attachments=True)))
        self.assertIn("link", result.detail.lower())
        self.assertIn("attach", result.detail.lower())

    def test_the_title_and_topic_survive_the_fallback(self):
        materials = FakeMaterials(refuse_attachments=True)
        post(service_with(materials), topic_id="topic-9")
        second = materials.bodies[1]
        self.assertEqual(second["title"], "Session 139 — Day 2 (Jul 22)")
        self.assertEqual(second["topicId"], "topic-9")

    def test_an_existing_description_is_kept_above_the_link(self):
        materials = FakeMaterials(refuse_attachments=True)
        post(service_with(materials), description="Recorded 22 July.")
        text = materials.bodies[1]["description"]
        self.assertTrue(text.startswith("Recorded 22 July."))
        self.assertIn("drive.google.com", text)

    def test_reports_the_original_error_when_the_fallback_also_fails(self):
        """A second, more confusing error is worse than the first one."""
        result = post(service_with(FakeMaterials(refuse_everything=True)))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "permission_denied")

    def test_no_fallback_without_links_to_fall_back_to(self):
        materials = FakeMaterials(refuse_attachments=True)
        result = post(service_with(materials), drive_links=None)
        self.assertFalse(result.ok)
        self.assertEqual(len(materials.bodies), 1)


class TestThe403Explanation(unittest.TestCase):
    def test_points_at_file_access_not_at_a_wrong_guess(self):
        """The course picker only lists courses the account teaches, and the
        API is provably enabled if courses were listed — so neither belongs in
        this message."""
        result = ClassroomService._explain(
            http_error(403, "The caller does not have permission")
        )
        self.assertEqual(result.reason, "permission_denied")
        self.assertIn("share the video file", result.detail)
        self.assertNotIn("isn't enabled", result.detail)
        self.assertNotIn("not a teacher", result.detail)

    def test_a_disabled_api_is_still_reported_as_such(self):
        result = ClassroomService._explain(
            http_error(403, "Google Classroom API has not been used in project 668249165369")
        )
        self.assertEqual(result.reason, "api_not_enabled")
        self.assertIn("668249165369", result.detail)

    def test_attachment_not_visible_is_caught_at_its_real_status(self):
        """Google returns this as FAILED_PRECONDITION (400), not 403 — testing
        it inside the 403 branch meant it never matched."""
        result = ClassroomService._explain(
            http_error(400, "AttachmentNotVisible", extra="FAILED_PRECONDITION")
        )
        self.assertEqual(result.reason, "attachment_not_visible")


class TestGrantingTheTeacherAccess(unittest.TestCase):
    def test_the_worker_grants_before_posting(self):
        """Ordering matters: granting after the post is useless."""
        from services import publish_worker
        import inspect

        source = inspect.getsource(publish_worker.run_publish_job)
        self.assertLess(
            source.index("grant_access"),
            source.index("post_material"),
            "access must be granted before Classroom tries to attach the file",
        )

    def test_grant_access_is_a_no_op_without_an_email(self):
        from services.drive_service import DriveService

        svc = DriveService()
        self.assertFalse(svc.grant_access("file-1", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
