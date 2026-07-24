"""
Reads what it can out of a Google Classroom course name.

Your course names already carry the facts the Class settings screen asks for:

    AALB Medical Interpreter Training
    (Session 139 Schedule (Accelerated): Mondays, Wednesdays, and Fridays
     (Night | July 17th to August 8th, 2026))
                 │        │                    │
                 │        │                    └── term dates -> first class date
                 │        └── which days it meets -> meeting weekdays
                 └── session number

So picking the course can fill in most of the form. Everything returned here is
a *suggestion* the user can overwrite — nothing is applied silently, and a name
that doesn't parse returns empty fields rather than guesses.

Deliberately NOT inferred: class start and end times. "Night" is in the title,
but 11pm is not, and inventing a time would produce silently wrong trims.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

PARTS_OF_DAY = ["morning", "afternoon", "evening", "night", "day"]

_MONTH_DAY = r"(" + "|".join(MONTH_NAMES) + r")\s+(\d{1,2})(?:st|nd|rd|th)?"
_RANGE_RE = re.compile(
    _MONTH_DAY + r"\s*(?:to|–|—|-|through|until)\s*" + _MONTH_DAY + r"\s*,?\s*(\d{4})?",
    re.IGNORECASE,
)


@dataclass
class ParsedCourse:
    """What could be read from a course name. Empty fields mean 'not stated'."""

    session_code: Optional[str] = None
    meeting_weekdays: List[int] = None            # 0=Mon .. 6=Sun
    first_class_date: Optional[str] = None        # "2026-07-17"
    term_start: Optional[str] = None
    term_end: Optional[str] = None
    part_of_day: Optional[str] = None             # "night", "day", ...
    label: Optional[str] = None                   # tidy name for the class

    def __post_init__(self) -> None:
        if self.meeting_weekdays is None:
            self.meeting_weekdays = []

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _parse_date(month_word: str, day: str, year: int) -> Optional[date]:
    month = MONTH_NAMES.get(month_word.lower())
    if not month:
        return None
    try:
        return date(year, month, int(day))
    except ValueError:                              # e.g. "February 31st"
        return None


def first_meeting_on_or_after(start: date, weekdays: List[int]) -> Optional[date]:
    """
    The first day the class actually meets, on or after the term start.

    A term that begins on a Wednesday for a Mon/Wed/Fri class starts on that
    Wednesday; one that begins on a Sunday starts the following Monday. Getting
    this right is what makes day numbers line up.
    """
    if not weekdays:
        return None
    for offset in range(7):
        candidate = start + timedelta(days=offset)
        if candidate.weekday() in weekdays:
            return candidate
    return None


def parse_course_name(name: str, fallback_year: Optional[int] = None) -> ParsedCourse:
    """Pull session number, meeting days and term dates out of a course name."""
    parsed = ParsedCourse()
    if not name:
        return parsed

    # --- session number ---------------------------------------------------
    match = re.search(r"session\s*#?\s*(\d{3})", name, re.IGNORECASE)
    if match:
        parsed.session_code = match.group(1)

    # --- which days it meets ---------------------------------------------
    found: List[int] = []
    for word, index in WEEKDAY_NAMES.items():
        if re.search(rf"\b{word}s?\b", name, re.IGNORECASE) and index not in found:
            found.append(index)
    parsed.meeting_weekdays = sorted(found)

    # --- part of day (a hint only; times are never inferred) --------------
    for part in PARTS_OF_DAY:
        if re.search(rf"\b{part}\b", name, re.IGNORECASE):
            parsed.part_of_day = part
            break

    # --- term dates -------------------------------------------------------
    range_match = _RANGE_RE.search(name)
    if range_match:
        start_month, start_day, end_month, end_day, year = range_match.groups()
        resolved_year = int(year) if year else fallback_year
        if resolved_year:
            start = _parse_date(start_month, start_day, resolved_year)
            end = _parse_date(end_month, end_day, resolved_year)
            # A range that ends before it starts has rolled into the next year.
            if start and end and end < start:
                end = _parse_date(end_month, end_day, resolved_year + 1)
            if start:
                parsed.term_start = start.isoformat()
                first = first_meeting_on_or_after(start, parsed.meeting_weekdays)
                parsed.first_class_date = (first or start).isoformat()
            if end:
                parsed.term_end = end.isoformat()

    # --- a tidy label -----------------------------------------------------
    if parsed.session_code:
        days = "".join(
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d][:3] + "/"
            for d in parsed.meeting_weekdays
        ).rstrip("/")
        bits = [f"Session {parsed.session_code}"]
        if days:
            bits.append(days)
        if parsed.part_of_day:
            bits.append(parsed.part_of_day.capitalize())
        parsed.label = " — ".join([bits[0], " ".join(bits[1:])]) if len(bits) > 1 else bits[0]

    return parsed
