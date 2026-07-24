"""
Tests for the publish planning maths.

These cover the arithmetic that used to live in React with console.logs: where
to cut, which class day it is, and what the resulting files are called. All
pure functions — no network, no Google, no Zoom.

Run:  cd backend && python -m pytest tests/test_publish_planner.py -q
      (or python tests/test_publish_planner.py for a plain-stdlib run)
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.class_config import ClassSettings, PublishConfig  # noqa: E402
from services.publish_planner import (  # noqa: E402
    compute_trim,
    extract_session_code,
    format_time,
    plan_recording,
)

UTC = ZoneInfo("UTC")


def night_class(**overrides) -> ClassSettings:
    """Session 127: 11pm-2am Eastern, Mon/Wed/Fri, first class Nov 10 2025."""
    base = dict(
        code="127",
        label="Session 127 — Mon/Wed/Fri Night",
        timezone="America/New_York",
        scheduled_start="23:00",
        scheduled_end="02:00",
        meeting_weekdays=[0, 2, 4],
        first_class_date="2025-11-10",
        pad_before_minutes=1,
        pad_after_minutes=5,
        classroom_course_name="Session 127 (Night)",
    )
    base.update(overrides)
    return ClassSettings(**base)


def day_class(**overrides) -> ClassSettings:
    base = dict(
        code="128",
        label="Session 128 — Tue/Thu Day",
        timezone="America/New_York",
        scheduled_start="10:00",
        scheduled_end="13:00",
        meeting_weekdays=[1, 3],
        first_class_date="2025-11-11",
        pad_before_minutes=2,
        pad_after_minutes=5,
    )
    base.update(overrides)
    return ClassSettings(**base)


class TestSessionCode(unittest.TestCase):
    def test_extracts_from_real_titles(self):
        self.assertEqual(
            extract_session_code(
                "Session 127. Mondays, Wednesdays, and Fridays, "
                "(Night | November 10th to December 22nd, 2025)"
            ),
            "127",
        )
        self.assertEqual(extract_session_code("session  128 whatever"), "128")

    def test_none_when_absent(self):
        self.assertIsNone(extract_session_code("Makeup class Nov 15"))
        self.assertIsNone(extract_session_code(""))
        self.assertIsNone(extract_session_code("Session 12"))   # needs 3 digits


class TestDayNumber(unittest.TestCase):
    def test_counts_meeting_days_only(self):
        c = night_class()
        self.assertEqual(c.day_number_for(date(2025, 11, 10)), 1)   # Mon
        self.assertEqual(c.day_number_for(date(2025, 11, 12)), 2)   # Wed
        self.assertEqual(c.day_number_for(date(2025, 11, 14)), 3)   # Fri
        self.assertEqual(c.day_number_for(date(2025, 11, 17)), 4)   # Mon
        self.assertEqual(c.day_number_for(date(2025, 11, 19)), 5)   # Wed

    def test_non_meeting_day_is_none(self):
        self.assertIsNone(night_class().day_number_for(date(2025, 11, 11)))  # Tue

    def test_before_start_is_none(self):
        self.assertIsNone(night_class().day_number_for(date(2025, 11, 3)))

    def test_unconfigured_class_is_none(self):
        self.assertIsNone(ClassSettings(code="999").day_number_for(date(2025, 11, 19)))

    def test_two_day_a_week_class(self):
        c = day_class()
        self.assertEqual(c.day_number_for(date(2025, 11, 11)), 1)   # Tue
        self.assertEqual(c.day_number_for(date(2025, 11, 13)), 2)   # Thu
        self.assertEqual(c.day_number_for(date(2025, 11, 18)), 3)   # Tue
        self.assertEqual(c.day_number_for(date(2025, 11, 20)), 4)   # Thu


class TestScheduledDuration(unittest.TestCase):
    def test_normal_window(self):
        self.assertEqual(day_class().scheduled_duration_minutes(), 180)

    def test_crosses_midnight(self):
        self.assertEqual(night_class().scheduled_duration_minutes(), 180)

    def test_unset(self):
        self.assertIsNone(ClassSettings(code="1").scheduled_duration_minutes())


class TestComputeTrim(unittest.TestCase):
    def test_host_opened_room_early(self):
        # Class at 11:00pm ET; host started recording 10:55pm ET (03:55 UTC).
        start = datetime(2025, 11, 20, 3, 55, tzinfo=UTC)
        trim = compute_trim(night_class(), start, video_duration_seconds=3 * 3600 + 17 * 60)
        self.assertEqual(trim["source"], "schedule")
        # 5 min in, minus 1 min padding = 4 min
        self.assertAlmostEqual(trim["start_seconds"], 240, delta=1)
        # 5 min offset + 180 min class + 5 min padding = 190 min
        self.assertAlmostEqual(trim["end_seconds"], 190 * 60, delta=1)

    def test_end_clamped_to_video_length(self):
        start = datetime(2025, 11, 20, 3, 55, tzinfo=UTC)
        trim = compute_trim(night_class(), start, video_duration_seconds=60 * 60)
        self.assertEqual(trim["end_seconds"], 3600)

    def test_recording_started_late(self):
        # Started 10 min after the scheduled start.
        start = datetime(2025, 11, 20, 4, 10, tzinfo=UTC)
        trim = compute_trim(night_class(), start, video_duration_seconds=3 * 3600)
        self.assertEqual(trim["source"], "schedule")
        self.assertEqual(trim["start_seconds"], 0.0)   # can't go before the file

    def test_absurd_offset_falls_back_to_full(self):
        # Recording at 6am ET has nothing to do with an 11pm class.
        start = datetime(2025, 11, 20, 11, 0, tzinfo=UTC)
        trim = compute_trim(night_class(), start, video_duration_seconds=5400)
        self.assertEqual(trim["source"], "unmatched")
        self.assertEqual(trim["start_seconds"], 0.0)
        self.assertEqual(trim["end_seconds"], 5400)

    def test_no_schedule_keeps_everything(self):
        trim = compute_trim(
            ClassSettings(code="127"), datetime(2025, 11, 20, 3, 55, tzinfo=UTC), 5400
        )
        self.assertEqual(trim["source"], "full")
        self.assertEqual(trim["end_seconds"], 5400)

    def test_daytime_class(self):
        # 10:00am ET class, recording opened 09:58am ET (14:58 UTC).
        start = datetime(2025, 11, 18, 14, 58, tzinfo=UTC)
        trim = compute_trim(day_class(), start, video_duration_seconds=3 * 3600 + 600)
        self.assertEqual(trim["source"], "schedule")
        self.assertEqual(trim["start_seconds"], 0.0)      # 2 min in - 2 min pad
        self.assertAlmostEqual(trim["end_seconds"], (2 + 180 + 5) * 60, delta=1)

    def test_never_produces_inverted_range(self):
        for hour in range(24):
            start = datetime(2025, 11, 20, hour, 0, tzinfo=UTC)
            trim = compute_trim(night_class(), start, video_duration_seconds=7200)
            self.assertLess(
                trim["start_seconds"], trim["end_seconds"],
                f"inverted trim for recording at {hour}:00 UTC",
            )


def recording(**overrides):
    base = {
        "id": "rec-abc",
        "meeting_id": "881122",
        "topic": "Session 127. Mondays, Wednesdays, and Fridays, (Night)",
        "start_time": "2025-11-20T03:55:00Z",
        "duration": 197,
        "host_name": "Dana R",
        "recording_files": [
            {"id": "f1", "file_type": "MP4", "file_size": 1_800_000_000,
             "download_url": "https://zoom.example/a", "recording_type": "shared_screen_with_speaker_view"},
            {"id": "f2", "file_type": "MP4", "file_size": 1_100_000_000,
             "download_url": "https://zoom.example/b", "recording_type": "shared_screen_with_gallery_view"},
            {"id": "f3", "file_type": "M4A", "file_size": 80_000_000,
             "download_url": "https://zoom.example/c", "recording_type": "audio_only"},
        ],
    }
    base.update(overrides)
    return base


def config_with(*classes) -> PublishConfig:
    return PublishConfig(classes={c.code: c for c in classes}, classroom_subject="teacher@aalb.org")


class TestPlanRecording(unittest.TestCase):
    def test_full_plan_for_configured_class(self):
        plan = plan_recording(recording(), config_with(night_class()))
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(plan["session_code"], "127")
        self.assertEqual(plan["day_number"], 5)          # Wed Nov 19 local
        self.assertEqual(plan["date_key"], "Nov19")
        self.assertEqual(len(plan["outputs"]), 1)        # speaker only by default
        self.assertEqual(plan["outputs"][0]["key"], "speaker")
        self.assertEqual(
            plan["outputs"][0]["filename"],
            "Session 127 - Day 5 - Nov19 (Speaker + Screenshare).mp4",
        )
        self.assertEqual(plan["title"], "Session 127 (Night) — Day 5 (Nov19)")

    def test_local_date_not_utc_date(self):
        # 03:55 UTC on Nov 20 is 10:55pm on Nov 19 in New York. The day number
        # and filename must both use the local date, or every night class is
        # labelled with the wrong day.
        plan = plan_recording(recording(), config_with(night_class()))
        self.assertEqual(plan["date_key"], "Nov19")
        self.assertEqual(plan["day_number"], 5)

    def test_gallery_added_when_class_wants_it(self):
        plan = plan_recording(
            recording(), config_with(night_class(views=["speaker", "gallery"]))
        )
        self.assertEqual([o["key"] for o in plan["outputs"]], ["speaker", "gallery"])
        self.assertIn("Gallery", plan["outputs"][1]["filename"])

    def test_requested_view_missing_from_zoom_is_skipped(self):
        rec = recording(recording_files=[
            {"id": "f1", "file_type": "MP4", "file_size": 1,
             "download_url": "u", "recording_type": "shared_screen_with_speaker_view"},
        ])
        plan = plan_recording(rec, config_with(night_class(views=["speaker", "gallery"])))
        self.assertEqual([o["key"] for o in plan["outputs"]], ["speaker"])

    def test_unmatched_recording_is_flagged_not_crashed(self):
        plan = plan_recording(recording(topic="Makeup class Nov 15"), config_with(night_class()))
        self.assertFalse(plan["ready"])
        self.assertIn("no_session_code", plan["blockers"])
        self.assertIsNone(plan["session_code"])

    def test_known_code_but_no_settings_yet(self):
        plan = plan_recording(recording(), PublishConfig())
        self.assertFalse(plan["ready"])
        self.assertIn("class_not_configured", plan["blockers"])
        self.assertEqual(plan["session_code"], "127")

    def test_overrides_apply(self):
        plan = plan_recording(
            recording(topic="Makeup class"),
            config_with(night_class()),
            day_override=9,
            session_override="127",
        )
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["day_number"], 9)
        self.assertIn("Day 9", plan["outputs"][0]["filename"])

    def test_day_number_missing_is_a_blocker_not_a_zero(self):
        # The old code silently shipped "Day 0". A wrong day in a filename is
        # worse than being asked.
        plan = plan_recording(
            recording(start_time="2025-11-18T04:00:00Z"),   # Mon Nov 17 local... a meeting day
            config_with(night_class(meeting_weekdays=[5])),  # ...but class meets Saturdays
        )
        self.assertIsNone(plan["day_number"])
        self.assertIn("no_day_number", plan["blockers"])
        self.assertFalse(plan["ready"])

    def test_no_video_files(self):
        plan = plan_recording(recording(recording_files=[]), config_with(night_class()))
        self.assertIn("no_video_files", plan["blockers"])
        self.assertEqual(plan["outputs"], [])

    def test_bad_filename_pattern_does_not_explode(self):
        plan = plan_recording(
            recording(), config_with(night_class(filename_pattern="{nope} - {day}.mp4"))
        )
        # Falls back to the default rather than raising.
        self.assertIn("Day 5", plan["outputs"][0]["filename"])


class TestFormatTime(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_time(0), "0:00:00")
        self.assertEqual(format_time(59), "0:00:59")
        self.assertEqual(format_time(3661), "1:01:01")
        self.assertEqual(format_time(11820), "3:17:00")
        self.assertEqual(format_time(-5), "0:00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
