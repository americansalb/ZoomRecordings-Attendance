"""
Tests for reading class details out of a Google Classroom course name.

The names here are the real shapes from the AALB Classroom account.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.course_parser import (  # noqa: E402
    first_meeting_on_or_after,
    parse_course_name,
)

REAL = (
    "AALB Medical Interpreter Training (Session 139 Schedule (Accelerated): "
    "Mondays, Wednesdays, and Fridays (Night | July 17th to August 8th, 2026))"
)


class TestRealCourseName(unittest.TestCase):
    def setUp(self):
        self.p = parse_course_name(REAL)

    def test_session_number(self):
        self.assertEqual(self.p.session_code, "139")

    def test_meeting_days(self):
        self.assertEqual(self.p.meeting_weekdays, [0, 2, 4])      # Mon, Wed, Fri

    def test_term_dates(self):
        self.assertEqual(self.p.term_start, "2026-07-17")
        self.assertEqual(self.p.term_end, "2026-08-08")

    def test_first_class_date_is_a_meeting_day(self):
        # July 17 2026 is a Friday, which is a meeting day, so it stands.
        self.assertEqual(self.p.first_class_date, "2026-07-17")
        self.assertEqual(date.fromisoformat(self.p.first_class_date).weekday(), 4)

    def test_part_of_day(self):
        self.assertEqual(self.p.part_of_day, "night")

    def test_times_are_never_invented(self):
        # "Night" is in the title; 11pm is not. Guessing would produce silently
        # wrong trims, so no time fields exist on the result at all.
        self.assertFalse(hasattr(self.p, "scheduled_start"))


class TestOtherShapes(unittest.TestCase):
    def test_two_day_week(self):
        p = parse_course_name(
            "Session 140 Schedule: Tuesdays and Thursdays "
            "(Night | August 6th to October 8th, 2026)"
        )
        self.assertEqual(p.session_code, "140")
        self.assertEqual(p.meeting_weekdays, [1, 3])
        self.assertEqual(p.first_class_date, "2026-08-06")        # a Thursday

    def test_weekend_class(self):
        p = parse_course_name(
            "Session 136 Schedule: Saturdays and Sundays "
            "(Morning | May 17th to July 25th, 2026)"
        )
        self.assertEqual(p.meeting_weekdays, [5, 6])
        self.assertEqual(p.part_of_day, "morning")
        self.assertEqual(p.first_class_date, "2026-05-17")        # a Sunday

    def test_term_start_not_a_meeting_day_rolls_forward(self):
        # Term opens Monday Jul 20 2026 but the class meets Tue/Thu.
        p = parse_course_name(
            "Session 141: Tuesdays and Thursdays (July 20th to August 20th, 2026)"
        )
        self.assertEqual(p.term_start, "2026-07-20")
        self.assertEqual(p.first_class_date, "2026-07-21")        # the Tuesday
        self.assertEqual(date.fromisoformat(p.first_class_date).weekday(), 1)

    def test_range_crossing_new_year(self):
        p = parse_course_name(
            "Session 145: Mondays (November 10th to January 12th, 2026)"
        )
        self.assertEqual(p.term_start, "2026-11-10")
        self.assertEqual(p.term_end, "2027-01-12")                # rolled forward

    def test_label_is_readable(self):
        p = parse_course_name(REAL)
        self.assertIn("Session 139", p.label)
        self.assertIn("Night", p.label)


class TestDegradesQuietly(unittest.TestCase):
    """A name that says nothing must return nothing, not a guess."""

    def test_no_session_number(self):
        p = parse_course_name("General Staff Meetings")
        self.assertIsNone(p.session_code)
        self.assertEqual(p.meeting_weekdays, [])
        self.assertIsNone(p.first_class_date)
        self.assertIsNone(p.label)

    def test_empty(self):
        p = parse_course_name("")
        self.assertIsNone(p.session_code)
        self.assertEqual(p.meeting_weekdays, [])

    def test_days_without_dates(self):
        p = parse_course_name("Session 150: Mondays and Wednesdays")
        self.assertEqual(p.session_code, "150")
        self.assertEqual(p.meeting_weekdays, [0, 2])
        self.assertIsNone(p.first_class_date)     # no dates stated, none invented

    def test_dates_without_a_year_are_ignored_unless_told(self):
        p = parse_course_name("Session 151: Fridays (March 3rd to April 4th)")
        self.assertIsNone(p.first_class_date)
        p2 = parse_course_name("Session 151: Fridays (March 3rd to April 4th)", fallback_year=2026)
        self.assertEqual(p2.term_start, "2026-03-03")

    def test_impossible_date_is_not_accepted(self):
        p = parse_course_name("Session 152: Mondays (February 31st to March 5th, 2026)")
        self.assertIsNone(p.term_start)

    def test_two_digit_session_is_not_matched(self):
        self.assertIsNone(parse_course_name("Session 99: Mondays").session_code)


class TestFirstMeeting(unittest.TestCase):
    def test_start_is_already_a_meeting_day(self):
        self.assertEqual(
            first_meeting_on_or_after(date(2026, 7, 17), [0, 2, 4]), date(2026, 7, 17)
        )

    def test_rolls_to_the_next_meeting_day(self):
        # Saturday Jul 18 -> Monday Jul 20 for a Mon/Wed/Fri class
        self.assertEqual(
            first_meeting_on_or_after(date(2026, 7, 18), [0, 2, 4]), date(2026, 7, 20)
        )

    def test_no_weekdays_means_no_answer(self):
        self.assertIsNone(first_meeting_on_or_after(date(2026, 7, 17), []))


if __name__ == "__main__":
    unittest.main(verbosity=2)
