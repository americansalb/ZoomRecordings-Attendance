"""
Tests for posting a recording to Google Classroom.

The video is posted as a *link* to the Drive file, never as an attached Drive
file. Attaching makes Classroom re-share the file on the teacher's behalf, and
the teacher has no rights over a file the service account owns — which is what
Google was refusing with a bare 403 "The caller does not have permission".
Sharing is Drive's job here: the file is already "anyone with the link can
view", so students need no sign-in and Classroom needs no permission.

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
    """Refuses posts carrying materials if `refuse_materials`."""

    def __init__(self, refuse_materials: bool = False, refuse_everything: bool = False):
        self.bodies: List[Dict[str, Any]] = []
        self.refuse_materials = refuse_materials
        self.refuse_everything = refuse_everything

    def error_for(self, body) -> Optional[HttpError]:
        if self.refuse_everything:
            return http_error(403, "The caller does not have permission")
        if self.refuse_materials and body.get("materials"):
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


class TestThePostIsALink(unittest.TestCase):
    def test_posts_a_link_material(self):
        materials = FakeMaterials()
        result = post(service_with(materials))
        self.assertTrue(result.ok)
        self.assertEqual(
            materials.bodies[0]["materials"],
            [{"link": {"url": "https://drive.google.com/file/d/file-1/view"}}],
        )

    def test_never_attaches_the_drive_file(self):
        """The attachment is the thing Google refuses — it must not come back."""
        materials = FakeMaterials()
        post(service_with(materials))
        for body in materials.bodies:
            for material in body.get("materials", []):
                self.assertNotIn("driveFile", material)

    def test_a_link_is_built_from_the_file_id_when_none_was_given(self):
        materials = FakeMaterials()
        post(service_with(materials), drive_links=None)
        self.assertEqual(
            materials.bodies[0]["materials"][0]["link"]["url"],
            "https://drive.google.com/file/d/file-1/view",
        )

    def test_posts_every_video_that_was_uploaded(self):
        materials = FakeMaterials()
        post(service_with(materials), drive_links=["https://a/1", "https://b/2"])
        self.assertEqual(len(materials.bodies[0]["materials"]), 2)

    def test_only_posts_once_when_it_works(self):
        materials = FakeMaterials()
        post(service_with(materials))
        self.assertEqual(len(materials.bodies), 1)

    def test_no_fallback_marker_on_a_clean_post(self):
        result = post(service_with(FakeMaterials()))
        self.assertIsNone(result.reason)


class TestWhenEvenTheLinkMaterialIsRefused(unittest.TestCase):
    """Classroom can resolve a Drive URL back into the file it points at, which
    lands on the sharing path again. Plain text in the description cannot be
    resolved into anything."""

    def test_falls_back_to_the_link_in_the_description(self):
        materials = FakeMaterials(refuse_materials=True)
        result = post(service_with(materials))

        self.assertTrue(result.ok, "the class should still get the recording")
        self.assertEqual(result.reason, "posted_as_link")
        second = materials.bodies[1]
        self.assertNotIn("materials", second)
        self.assertIn("https://drive.google.com/file/d/file-1/view", second["description"])

    def test_the_title_and_topic_survive_the_fallback(self):
        materials = FakeMaterials(refuse_materials=True)
        post(service_with(materials), topic_id="topic-9")
        second = materials.bodies[1]
        self.assertEqual(second["title"], "Session 139 — Day 2 (Jul 22)")
        self.assertEqual(second["topicId"], "topic-9")

    def test_an_existing_description_is_kept_above_the_link(self):
        materials = FakeMaterials(refuse_materials=True)
        post(service_with(materials), description="Recorded 22 July.")
        text = materials.bodies[1]["description"]
        self.assertTrue(text.startswith("Recorded 22 July."))
        self.assertIn("drive.google.com", text)

    def test_reports_the_original_error_when_the_fallback_also_fails(self):
        """A second, more confusing error is worse than the first one."""
        result = post(service_with(FakeMaterials(refuse_everything=True)))
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "permission_denied")


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


class TestDriveOnly(unittest.TestCase):
    """Choosing Drive on its own must not touch Classroom at all."""

    def _run_worker(self, drive_only: bool):
        from services import publish_worker
        from tests.test_drive_upload import FakeJobStore, a_video, NAME, PATH

        request = {
            "outputs": [{"key": "gallery", "folder": "Gallery + Screenshare",
                         "filename": NAME, "drive_folders": PATH,
                         "download_url": "https://zoom/x"}],
            "session_code": "139", "start_seconds": 0, "end_seconds": 100,
            "title": "Day 2", "course_id": "course-1", "drive_only": drive_only,
        }
        store = FakeJobStore(request)
        clip = a_video()
        posted: List[Any] = []
        granted: List[Any] = []

        class FakeTrimmer:
            def __init__(self, output_dir=None): pass
            def trim_from_source(self, **kw): return clip
            def cleanup(self): pass

        class FakeDrive:
            def ensure_path(self, folder_names): return "f"
            def grant_access(self, file_id, email, role="writer"):
                granted.append(file_id)
                return True
            def upload_to_path(self, **kw):
                return {"file_id": "f1", "name": NAME,
                        "web_view_link": "https://drive/f1", "folder_path": PATH}

        class FakeClassroom:
            def post_material(self, **kw):
                posted.append(kw)
                from services.classroom_service import ClassroomResult
                return ClassroomResult(ok=True, material_id="m1")

        patches = {"get_job_store": lambda: store, "VideoTrimmerService": FakeTrimmer,
                   "drive_service": FakeDrive(), "classroom_service": FakeClassroom()}
        originals = {k: getattr(publish_worker, k) for k in patches}
        for k, v in patches.items():
            setattr(publish_worker, k, v)
        try:
            publish_worker.run_publish_job("job-1")
        finally:
            for k, v in originals.items():
                setattr(publish_worker, k, v)
        return store, posted, granted

    def test_classroom_is_never_called(self):
        _, posted, _ = self._run_worker(drive_only=True)
        self.assertEqual(posted, [])

    def test_the_teacher_is_not_granted_access_either(self):
        """Nothing was shared with Classroom, so nothing needs sharing."""
        _, _, granted = self._run_worker(drive_only=True)
        self.assertEqual(granted, [])

    def test_it_finishes_as_a_success_not_a_partial_failure(self):
        store, _, _ = self._run_worker(drive_only=True)
        final = store.final()
        self.assertEqual(final["status"], "completed")
        self.assertNotIn("Not posted", final["message"])
        self.assertEqual(final["result"]["classroom"]["reason"], "drive_only")

    def test_the_normal_path_still_posts(self):
        store, posted, granted = self._run_worker(drive_only=False)
        self.assertEqual(len(posted), 1)
        self.assertEqual(granted, ["f1"])
        self.assertEqual(store.final()["status"], "completed")


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
