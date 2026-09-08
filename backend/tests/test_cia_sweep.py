"""
Tests for the CIA auto-upload sweep.

The matching, file picking, and naming are pure functions; the sweep
itself runs here against fake Zoom and Drive services, so the delivery
rules (skip what is already delivered, retry what Zoom is still
processing, stamp only after every upload succeeded, never link-share)
are pinned without any network.

Run:  cd backend && python -m pytest tests/test_cia_sweep.py -q
      (or python tests/test_cia_sweep.py for a plain-stdlib run)
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.cia_sweep import (  # noqa: E402
    exam_label,
    is_cia_topic,
    pick_audio,
    pick_video,
    run_cia_sweep,
    still_processing,
)


class TestTopicMatching(unittest.TestCase):
    def test_the_word_cia_matches_in_any_casing(self):
        self.assertTrue(is_cia_topic("CIA - Maria Lopez"))
        self.assertTrue(is_cia_topic("Maria Lopez cia attempt 2"))
        self.assertTrue(is_cia_topic("Final CIA (Gujarati)"))

    def test_the_phrase_matches_with_both_spellings(self):
        self.assertTrue(is_cia_topic("Consecutive Interpreting Assessment - J. Patel"))
        self.assertTrue(is_cia_topic("consecutive interpretation assessment attempt 1"))

    def test_names_and_classes_never_match(self):
        self.assertFalse(is_cia_topic("Session 142 - Day 3"))
        self.assertFalse(is_cia_topic("Marcia's practice hour"))
        self.assertFalse(is_cia_topic("Valencia Garcia orientation"))
        self.assertFalse(is_cia_topic(""))
        self.assertFalse(is_cia_topic(None))


class TestFilePicking(unittest.TestCase):
    def test_prefers_the_speaker_views_over_gallery(self):
        files = [
            {"file_type": "MP4", "recording_type": "gallery_view", "download_url": "u1", "status": "completed"},
            {"file_type": "MP4", "recording_type": "shared_screen_with_speaker_view", "download_url": "u2", "status": "completed"},
            {"file_type": "M4A", "recording_type": "audio_only", "download_url": "u3", "status": "completed"},
        ]
        self.assertEqual(pick_video(files)["download_url"], "u2")
        self.assertEqual(pick_audio(files)["download_url"], "u3")

    def test_a_file_still_processing_is_not_picked(self):
        files = [{"file_type": "MP4", "recording_type": "speaker_view", "download_url": "", "status": "processing"}]
        self.assertIsNone(pick_video(files))
        self.assertTrue(still_processing(files))

    def test_any_completed_mp4_beats_nothing(self):
        files = [{"file_type": "MP4", "recording_type": "something_new", "download_url": "u9"}]
        self.assertEqual(pick_video(files)["download_url"], "u9")


class TestLabel(unittest.TestCase):
    def test_topic_plus_eastern_date(self):
        # 01:30 UTC on Sep 9 is the evening of Sep 8 in New York.
        label = exam_label("CIA - Maria Lopez", "2026-09-09T01:30:00Z")
        self.assertEqual(label, "CIA - Maria Lopez - Sep8")

    def test_unparseable_date_keeps_the_topic(self):
        self.assertEqual(exam_label("CIA - X", "not a date"), "CIA - X")


class FakeZoom:
    def __init__(self, recordings):
        self._recordings = recordings

    def get_accounts(self):
        return [{"id": "default", "name": "Default"}]

    async def list_all_recordings(self, from_date, to_date, account_id=None):
        return self._recordings

    async def get_download_token(self, account_id=None):
        return "token123"


class FakeDrive:
    def __init__(self, already_delivered=()):
        self.already = set(already_delivered)
        self.created_folders = []
        self.uploads = []
        self.stamps = []

    def find_by_app_property(self, key, value):
        return {"id": "old"} if value in self.already else None

    def get_or_create_folder(self, name, parent_id):
        self.created_folders.append((name, parent_id))
        return f"folder_{len(self.created_folders)}"

    def upload_file_to_folder(self, file_path, parent_id, file_name, mimetype="video/mp4", progress_callback=None):
        self.uploads.append((parent_id, file_name, mimetype))
        return {"file_id": f"f{len(self.uploads)}", "name": file_name, "web_view_link": "", "replaced": False}

    def set_app_properties(self, file_id, props):
        self.stamps.append((file_id, props))

    def service_account_email(self):
        return "uploader@example.iam.gserviceaccount.com"


def fake_download(url, token, dest_path):
    with open(dest_path, "wb") as f:
        f.write(b"video bytes")
    return 11


def _recording(topic, uuid, files=None, start="2026-09-08T22:00:00Z"):
    return {
        "topic": topic, "uuid": uuid, "start_time": start,
        "recording_files": files if files is not None else [
            {"file_type": "MP4", "recording_type": "shared_screen_with_speaker_view",
             "download_url": "https://zoom.example/v.mp4", "status": "completed"},
            {"file_type": "M4A", "recording_type": "audio_only",
             "download_url": "https://zoom.example/a.m4a", "status": "completed"},
        ],
    }


class TestSweep(unittest.TestCase):
    def _run(self, zoom, drive):
        return asyncio.run(run_cia_sweep({"zoom": zoom, "drive": drive, "download": fake_download}))

    def test_delivers_a_new_exam_and_leaves_classes_alone(self):
        zoom = FakeZoom([
            _recording("Session 142 - Day 4", "uuid-class"),
            _recording("CIA - Maria Lopez", "uuid-cia"),
        ])
        drive = FakeDrive()
        summary = self._run(zoom, drive)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(len(summary["delivered"]), 1)
        # One exam folder, holding the video and the audio copy.
        self.assertEqual(len(drive.created_folders), 1)
        names = [u[1] for u in drive.uploads]
        self.assertIn("CIA - Maria Lopez - Sep8.mp4", names)
        self.assertIn("CIA - Maria Lopez - Sep8 (audio).m4a", names)
        # Stamped with the Zoom UUID, once, after the uploads.
        self.assertEqual(len(drive.stamps), 1)
        self.assertEqual(drive.stamps[0][1]["cia_zoom_uuid"], "uuid-cia")

    def test_an_already_delivered_exam_is_skipped_even_after_the_wizard_moved_it(self):
        zoom = FakeZoom([_recording("CIA - Maria Lopez", "uuid-cia")])
        drive = FakeDrive(already_delivered={"uuid-cia"})
        summary = self._run(zoom, drive)
        self.assertEqual(summary["already_there"], 1)
        self.assertEqual(summary["delivered"], [])
        self.assertEqual(drive.uploads, [])

    def test_a_recording_zoom_is_still_processing_waits_without_a_stamp(self):
        zoom = FakeZoom([_recording(
            "CIA - Maria Lopez", "uuid-cia",
            files=[{"file_type": "MP4", "recording_type": "speaker_view",
                    "download_url": "", "status": "processing"}],
        )])
        drive = FakeDrive()
        summary = self._run(zoom, drive)
        self.assertEqual(summary["waiting_on_zoom"], 1)
        self.assertEqual(drive.uploads, [])
        self.assertEqual(drive.stamps, [])

    def test_a_failed_upload_leaves_no_stamp_so_the_next_sweep_retries(self):
        zoom = FakeZoom([_recording("CIA - Maria Lopez", "uuid-cia")])
        drive = FakeDrive()

        def broken_upload(*a, **k):
            raise RuntimeError("Drive said no")

        drive.upload_file_to_folder = broken_upload
        summary = self._run(zoom, drive)
        self.assertEqual(summary["delivered"], [])
        self.assertEqual(drive.stamps, [])
        self.assertEqual(len(summary["errors"]), 1)

    def test_a_permission_error_names_the_account_to_share_with(self):
        zoom = FakeZoom([_recording("CIA - Maria Lopez", "uuid-cia")])
        drive = FakeDrive()

        def forbidden(*a, **k):
            raise RuntimeError("HttpError 404: file not found")

        drive.get_or_create_folder = forbidden
        summary = self._run(zoom, drive)
        self.assertIn("uploader@example.iam.gserviceaccount.com", summary["errors"][0])


if __name__ == "__main__":
    unittest.main()
