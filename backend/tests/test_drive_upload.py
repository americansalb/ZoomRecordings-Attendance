"""
Tests for uploading a published recording to Drive.

Two things are being protected here:

1. Re-publishing a recording must not destroy the file that is already there.
   The old code deleted first and created second, so a failed second step left
   nothing behind — and permanent delete in a shared drive needs rights the
   service account may not have, which is why a *second* send of the same
   video failed when the first had worked.

2. A failure must arrive as a sentence someone can act on. "Drive upload
   failed" meant reading server logs to learn anything at all.

Runs against a fake Drive API, so no credentials and no network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from googleapiclient.errors import HttpError  # noqa: E402

from services.drive_service import DriveService, DriveUploadError  # noqa: E402


# --------------------------------------------------------------------------
# a fake Drive API that records the calls made against it
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int):
        self.status = status
        self.reason = "error"


def http_error(status: int, reason: str = "", message: str = "") -> HttpError:
    body = json.dumps({
        "error": {
            "code": status,
            "message": message or reason or "error",
            "errors": [{"reason": reason}] if reason else [],
        }
    }).encode("utf-8")
    return HttpError(FakeResponse(status), body)


class FakeRequest:
    def __init__(self, result: Dict[str, Any], error: Optional[HttpError] = None,
                 chunks: int = 2, total: int = 1000):
        self._result = result
        self._error = error
        self._chunks = chunks
        self._total = total
        self._sent = 0
        self.retries_asked = []

    def execute(self):
        if self._error:
            raise self._error
        return self._result

    def next_chunk(self, num_retries=0):
        self.retries_asked.append(num_retries)
        if self._error:
            raise self._error
        self._sent += 1
        if self._sent >= self._chunks:
            return None, self._result
        return _Status(self._sent * (self._total // self._chunks), self._total), None


class _Status:
    def __init__(self, done, total):
        self.resumable_progress = done
        self.total_size = total


class FakeFiles:
    """Records every call. Optional per-verb errors."""

    def __init__(self, existing: Optional[List[Dict[str, Any]]] = None,
                 errors: Optional[Dict[str, HttpError]] = None):
        self.records: List[tuple] = []          # (verb, kwargs), in order
        self._existing = existing if existing is not None else []
        self._errors = errors or {}
        self.last_request: Optional[FakeRequest] = None

    @property
    def calls(self) -> List[str]:
        return [verb for verb, _ in self.records]

    def content_calls(self) -> List[str]:
        """
        Only the calls that carry the video itself.

        _set_file_permissions also issues files().update to restrict copying,
        so "was update called" on its own says nothing about how the file got
        there.
        """
        return [verb for verb, kw in self.records if "media_body" in kw]

    def kwargs_for(self, verb: str) -> Dict[str, Any]:
        return next(kw for v, kw in self.records if v == verb)

    def _make(self, verb: str, result: Dict[str, Any], **kw) -> FakeRequest:
        self.records.append((verb, kw))
        req = FakeRequest(result, self._errors.get(verb))
        self.last_request = req
        return req

    def list(self, **kw):
        return self._make("list", {"files": list(self._existing)}, **kw)

    def create(self, **kw):
        return self._make(
            "create",
            {"id": "new-file", "name": kw.get("body", {}).get("name", "x"),
             "webViewLink": "https://drive/new-file"},
            **kw,
        )

    def update(self, **kw):
        return self._make(
            "update",
            {"id": kw.get("fileId", "existing"), "name": "same",
             "webViewLink": "https://drive/existing"},
            **kw,
        )

    def delete(self, **kw):
        return self._make("delete", {}, **kw)


class FakePermissions:
    def create(self, **kw):
        return FakeRequest({"id": "perm"})


class FakeDrive:
    def __init__(self, files: FakeFiles):
        self._files = files
        self._permissions = FakePermissions()

    def files(self):
        return self._files

    def permissions(self):
        return self._permissions


def service_with(files: FakeFiles) -> DriveService:
    svc = DriveService()
    svc._drive_service = FakeDrive(files)
    # A folder already resolved, so tests focus on the file itself.
    svc._folder_cache["path_%s_Session 139" % DriveService.SHARED_FOLDER_ID] = "f1"
    svc._folder_cache["path_f1_Gallery + Screenshare"] = "f2"
    return svc


def a_video() -> str:
    path = os.path.join(tempfile.mkdtemp(), "clip.mp4")
    with open(path, "wb") as f:
        f.write(b"\x00" * 1024)
    return path


PATH = ["Session 139", "Gallery + Screenshare"]
NAME = "Session 139 - Day 2 - Jul22 (Gallery + Screenshare).mp4"


class TestReplacingAnExistingRecording(unittest.TestCase):
    """The regression: sending the same recording a second time."""

    def test_existing_file_is_updated_in_place_never_deleted(self):
        files = FakeFiles(existing=[{"id": "old-id", "name": NAME}])
        result = service_with(files).upload_to_path(a_video(), PATH, NAME)

        self.assertNotIn("delete", files.calls, "must not delete the good file")
        self.assertEqual(files.content_calls(), ["update"])
        self.assertTrue(result["replaced"])

    def test_the_file_id_survives_a_replace(self):
        """Anything already pointing at the file — a Classroom attachment, a
        shared link — has to keep working."""
        files = FakeFiles(existing=[{"id": "old-id", "name": NAME}])
        result = service_with(files).upload_to_path(a_video(), PATH, NAME)
        self.assertEqual(result["file_id"], "old-id")

    def test_a_first_upload_still_creates(self):
        files = FakeFiles(existing=[])
        result = service_with(files).upload_to_path(a_video(), PATH, NAME)
        self.assertEqual(files.content_calls(), ["create"])
        self.assertFalse(result["replaced"])
        self.assertEqual(result["file_id"], "new-file")

    def test_shared_drive_support_is_requested_on_every_call(self):
        files = FakeFiles(existing=[{"id": "old-id", "name": NAME}])
        service_with(files).upload_to_path(a_video(), PATH, NAME)
        for verb, kw in files.records:
            self.assertTrue(kw.get("supportsAllDrives"),
                            f"{verb} would miss files in a shared drive")

    def test_transient_errors_are_retried_during_the_upload(self):
        files = FakeFiles(existing=[])
        service_with(files).upload_to_path(a_video(), PATH, NAME)
        self.assertTrue(all(n > 0 for n in files.last_request.retries_asked),
                        "a multi-GB upload must ride out 5xx blips")


class TestFailuresExplainThemselves(unittest.TestCase):
    def _message_for(self, error: HttpError) -> str:
        files = FakeFiles(existing=[], errors={"create": error})
        with self.assertRaises(DriveUploadError) as caught:
            service_with(files).upload_to_path(a_video(), PATH, NAME)
        return str(caught.exception)

    def test_out_of_space_says_so(self):
        message = self._message_for(http_error(403, "storageQuotaExceeded"))
        self.assertIn("out of storage", message)

    def test_missing_permission_names_the_access_to_grant(self):
        message = self._message_for(http_error(403, "insufficientFilePermissions"))
        self.assertIn("Content manager", message)

    def test_missing_folder_points_at_sharing(self):
        message = self._message_for(http_error(404, "notFound"))
        self.assertIn("not shared", message)

    def test_rate_limit_says_to_wait(self):
        message = self._message_for(http_error(429, "userRateLimitExceeded"))
        self.assertIn("rate-limit", message)

    def test_server_error_says_to_send_again(self):
        message = self._message_for(http_error(500))
        self.assertIn("send", message.lower())

    def test_an_unknown_error_still_carries_googles_own_words(self):
        message = self._message_for(http_error(400, "badRequest", "Bad field: parents"))
        self.assertIn("Bad field: parents", message)

    def test_a_folder_failure_names_the_folder(self):
        files = FakeFiles(existing=[], errors={"create": http_error(403, "forbidden")})
        svc = DriveService()
        svc._drive_service = FakeDrive(files)          # no folder cache primed
        with self.assertRaises(DriveUploadError) as caught:
            svc.upload_to_path(a_video(), PATH, NAME)
        self.assertIn("Session 139", str(caught.exception))

    def test_never_returns_none_for_a_failure(self):
        """The old contract. None told the caller nothing, so it printed a
        generic sentence and the reason stayed in the logs."""
        files = FakeFiles(existing=[], errors={"create": http_error(403, "forbidden")})
        with self.assertRaises(DriveUploadError):
            service_with(files).upload_to_path(a_video(), PATH, NAME)


class TestBadInputIsCaughtBeforeGoogleIsCalled(unittest.TestCase):
    def test_missing_trimmed_file(self):
        files = FakeFiles()
        with self.assertRaises(DriveUploadError) as caught:
            service_with(files).upload_to_path("/no/such/clip.mp4", PATH, NAME)
        self.assertIn("missing", str(caught.exception))
        self.assertEqual(files.calls, [], "should not have called Drive at all")

    def test_empty_trimmed_file(self):
        path = os.path.join(tempfile.mkdtemp(), "empty.mp4")
        open(path, "wb").close()
        files = FakeFiles()
        with self.assertRaises(DriveUploadError) as caught:
            service_with(files).upload_to_path(path, PATH, NAME)
        self.assertIn("empty", str(caught.exception))
        self.assertEqual(files.calls, [])


class FakeJobStore:
    def __init__(self, request_data):
        self.job = {"request_data": request_data}
        self.updates: List[Dict[str, Any]] = []

    def get_job(self, job_id):
        return self.job

    def update_job(self, job_id, **kw):
        self.updates.append(kw)

    def final(self) -> Dict[str, Any]:
        return next(u for u in reversed(self.updates) if u.get("status"))


class TestTheWorkerSurfacesTheReason(unittest.TestCase):
    """End to end through run_publish_job, with Drive and the trimmer faked."""

    REQUEST = {
        "outputs": [{
            "key": "gallery",
            "folder": "Gallery + Screenshare",
            "filename": NAME,
            "drive_folders": PATH,
            "download_url": "https://zoom/example",
        }],
        "session_code": "139",
        "start_seconds": 0,
        "end_seconds": 100,
        "title": "Day 2",
    }

    def _run(self, upload, ensure=None):
        from services import publish_worker

        store = FakeJobStore(dict(self.REQUEST))
        clip = a_video()
        self.trims = 0
        outer = self

        class FakeTrimmer:
            def __init__(self, output_dir=None):
                pass

            def trim_from_source(self, **kw):
                outer.trims += 1
                return clip

            def cleanup(self):
                pass

        class FakeDriveService:
            def ensure_path(self, folder_names):
                return ensure(folder_names) if ensure else "folder-id"

            def upload_to_path(self, **kw):
                return upload(**kw)

        patches = {
            "get_job_store": lambda: store,
            "VideoTrimmerService": FakeTrimmer,
            "drive_service": FakeDriveService(),
        }
        originals = {k: getattr(publish_worker, k) for k in patches}
        for k, v in patches.items():
            setattr(publish_worker, k, v)
        try:
            publish_worker.run_publish_job("job-1")
        finally:
            for k, v in originals.items():
                setattr(publish_worker, k, v)
        return store

    def test_the_job_message_carries_the_explanation_not_a_placeholder(self):
        def boom(**kw):
            raise DriveUploadError(
                "Google Drive is out of storage, so it could not be saved."
            )

        final = self._run(boom).final()
        self.assertEqual(final["status"], "failed")
        self.assertIn("Gallery + Screenshare", final["message"])
        self.assertIn("out of storage", final["message"])

    def test_the_old_generic_sentence_is_gone(self):
        def boom(**kw):
            raise DriveUploadError("Google Drive is out of storage.")

        final = self._run(boom).final()
        self.assertNotEqual(
            final["message"],
            "Publish failed: Drive upload failed for Gallery + Screenshare.",
        )

    def test_a_folder_problem_is_caught_before_the_trim_starts(self):
        """Twenty minutes of trimming, then "permission denied", is the worst
        possible ordering."""
        def no_access(folder_names):
            raise DriveUploadError("The service account is not allowed to write there.")

        store = self._run(lambda **kw: None, ensure=no_access)
        self.assertEqual(self.trims, 0, "must not trim before checking access")
        self.assertEqual(store.final()["status"], "failed")
        self.assertIn("not allowed", store.final()["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
