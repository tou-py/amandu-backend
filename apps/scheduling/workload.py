"""
How much of a day is spoken for, derived rather than stored.

The sibling of availability.py, asking the opposite question: that one says when
a professional is still free, this one says how much of the shop's day already
has work on it. Nothing here is persisted either -- both are views of the
appointments table, and a column caching them would be another thing to keep
true.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta


def daily_load(spans, tz):
    """
    One entry per local day that has work on it: how many turns, and how many
    minutes of the day they occupy.

    `spans` is (start, end) pairs of aware datetimes. Which appointments belong
    in it is the caller's decision -- the rule about cancelled slots lives with
    the query, not here, so this function has nothing to know about status.

    MINUTES ARE A UNION, NEVER A SUM. Two professionals booked for the same hour
    is ONE busy hour: this measures when the shop is working, not capacity sold.
    Capacity is not something to divide by anyway -- Appointment.capacity is per
    slot and has no tenant default -- so a percentage would be a confident
    invention and there is deliberately none here.

    Days are LOCAL days in `tz`, matching how the agenda's own `from`/`to`
    filters read a calendar day. A booking that crosses local midnight has its
    MINUTES split between the two days it really occupies, but is COUNTED once,
    on the day it starts: it is one appointment, and nobody reading "3 turnos"
    would accept that one late booking made it 4.
    """
    pieces = defaultdict(list)
    counts = defaultdict(int)

    for start, end in spans:
        start = start.astimezone(tz)
        end = end.astimezone(tz)
        # A zero- or negative-length row cannot be drawn and cannot be worked.
        if end <= start:
            continue

        counts[start.date()] += 1

        cursor = start
        while cursor < end:
            day = cursor.date()
            # Built from the DATE rather than by adding 24 hours, so the two
            # days a year that are 23 or 25 hours long still break at their real
            # local midnight.
            boundary = datetime.combine(day + timedelta(days=1), time(0), tzinfo=tz)
            piece_end = min(end, boundary)
            pieces[day].append((cursor, piece_end))
            cursor = piece_end

    return [
        {
            'date': day,
            'count': counts.get(day, 0),
            'busy_minutes': _union_minutes(pieces.get(day, ())),
        }
        # Sorted, because a caller rendering a row per day should not have to.
        for day in sorted(set(pieces) | set(counts))
    ]


def _union_minutes(intervals):
    """
    The length of the union of `intervals`, in whole minutes.

    Sorted by start, then swept once. `covered` is how far the union already
    reaches; it only ever moves forward, because a short booking nested inside a
    long one ends EARLIER than one already counted, and letting the mark slide
    back would count that tail twice.
    """
    total = timedelta()
    covered = None

    for start, end in sorted(intervals):
        begin = start if covered is None or start > covered else covered
        if end > begin:
            total += end - begin
        if covered is None or end > covered:
            covered = end

    return int(total.total_seconds() // 60)
