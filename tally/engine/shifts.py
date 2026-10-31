"""Shift calendar.

Night is 18:00 to 04:00 and day is 04:00 to 18:00. A night shift belongs to the
date it starts, so a load at 01:30 on Tuesday counts towards Monday's night
shift. Any shift that starts on a Saturday or Sunday is a weekend shift.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import NamedTuple

from tally.engine.models import ShiftKind

DAY_START = time(4, 0)
NIGHT_START = time(18, 0)


class Shift(NamedTuple):
    reg: str
    date: date
    kind: ShiftKind


def shift_of(when: datetime) -> tuple[date, ShiftKind]:
    t = when.time()
    if t < DAY_START:
        start, kind = when.date() - timedelta(days=1), ShiftKind.NIGHT
    elif t >= NIGHT_START:
        start, kind = when.date(), ShiftKind.NIGHT
    else:
        start, kind = when.date(), ShiftKind.DAY
    if start.weekday() >= 5:
        kind = ShiftKind.WEEKEND
    return start, kind


def default_shift_kind(day: date) -> ShiftKind:
    """Shift for an invoice line that gives a date but no time or shift."""
    return ShiftKind.WEEKEND if day.weekday() >= 5 else ShiftKind.DAY
