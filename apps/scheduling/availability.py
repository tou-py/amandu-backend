"""
When a professional is free, derived rather than stored.

The three tables this reads answer different halves of the question:
WorkSchedule says when the door is open, TimeOff and Appointment say what has
already been taken out of that. Nothing here is persisted -- availability is a
view of those three, and a fourth table caching it would be a fourth thing to
keep true.
"""

from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from apps.scheduling.models import Appointment, TimeOff, WorkSchedule


def free_slots(professional, service, since, until, now=None):
    """
    Every start time between `since` and `until` (both inclusive, local dates)
    where `service` fits in `professional`'s day.

    Returns `{date: [aware datetime, ...]}`, one key per day in the range,
    including the days that came back empty -- a caller drawing a week needs to
    know a day was closed, and a missing key does not say that.

    Takes a range rather than a day because the query count is the same either
    way: three, no matter how wide the window. Called once per day it would be
    three per day.
    """
    tenant = professional.tenant
    zone = ZoneInfo(tenant.timezone)
    now = now or timezone.now()

    # Local midnight to local midnight. Built from the tenant's zone, not UTC,
    # because a day is a local thing: in Asunción the 20th starts three hours
    # after UTC says it does, and querying by UTC dates would take three hours
    # off one end and add them to the other.
    window_start = datetime.combine(since, time.min, tzinfo=zone)
    window_end = datetime.combine(until + timedelta(days=1), time.min, tzinfo=zone)

    week = defaultdict(list)
    for row in WorkSchedule.objects.filter(tenant=tenant, professional=professional):
        week[row.weekday].append((row.start_time, row.end_time))

    taken = _taken(tenant, professional, window_start, window_end)

    slots = {}
    day = since
    while day <= until:
        slots[day] = _slots_for_day(day, week[day.weekday()], service, zone, taken, now)
        day += timedelta(days=1)
    return slots


def _taken(tenant, professional, window_start, window_end):
    """
    Every stretch already spoken for, as (start, end) pairs. Two sources, one
    list: the calculation subtracts them identically, and telling apart a
    holiday from a booking is the caller's problem, not this one's.

    Overlap test is `start < window_end and end > window_start`, the standard
    half-open one: a booking that ends exactly at midnight does not intrude on
    the next day.
    """
    time_off = TimeOff.objects.filter(
        tenant=tenant,
        start__lt=window_end,
        end__gt=window_start,
    ).filter(
        # Null professional is the whole tenant shut, and that closes this
        # person too.
        Q(professional=professional) | Q(professional__isnull=True)
    )

    booked = Appointment.objects.filter(
        tenant=tenant,
        professional=professional,
        start__lt=window_end,
        end__gt=window_start,
    ).exclude(status=Appointment.Status.CANCELLED)

    return (
        [(row.start, row.end) for row in time_off]
        + [(row.start, row.end) for row in booked]
    )


def _slots_for_day(day, stretches, service, zone, taken, now):
    found = []
    for start_time, end_time in sorted(stretches):
        # Wall clock into instants. The stored times are local by design, so a
        # shop that opens at 09:00 keeps opening at 09:00 across a DST change
        # instead of drifting an hour twice a year.
        #
        # ponytail: on the one day a DST jump makes a local time ambiguous or
        # nonexistent, ZoneInfo resolves it silently rather than raising.
        # Paraguay dropped DST in 2024 and Brazil in 2019, so nothing in the
        # current market hits it. Revisit if a tenant appears in a zone that
        # still shifts.
        opens = datetime.combine(day, start_time, tzinfo=zone)
        closes = datetime.combine(day, end_time, tzinfo=zone)

        cursor = opens
        while cursor + service.duration <= closes:
            slot_end = cursor + service.duration
            blocked_until = _blocked_until(cursor, slot_end, taken)

            if blocked_until is not None:
                # Resume where the obstacle ends, not one slot later. Packing
                # against what is already booked is what keeps the dead gaps out
                # of the day; stepping over by a fixed slot would invent them.
                #
                # Back into the tenant's zone, because that end came off a
                # database row and Django hands those back in UTC. The instant
                # would be right either way, but every slot after the first
                # obstacle would then carry a different offset from the ones
                # before it -- one list, two representations, and a public page
                # rendering "12:30+00:00" for a shop that means half past nine.
                cursor = blocked_until.astimezone(zone)
            elif cursor < now:
                # A slot in the past is not on offer. Nothing further back can
                # be either, but the loop still has to walk forward to today.
                cursor = slot_end
            else:
                found.append(cursor)
                cursor = slot_end
    return found


def _blocked_until(start, end, taken):
    """
    When the obstacle overlapping [start, end) clears, or None if nothing does.
    Returns the furthest end among the overlaps, so back-to-back bookings are
    stepped over in one go.

    ponytail: linear scan per slot. One professional over a week is a handful of
    rows against a few dozen slots; if a public page ever asks for a quarter at a
    time, sort `taken` once and walk it alongside the cursor.
    """
    clear = None
    for busy_start, busy_end in taken:
        if busy_start < end and busy_end > start:
            if clear is None or busy_end > clear:
                clear = busy_end
    return clear


OUTSIDE_HOURS = 'outside_working_hours'
TIME_OFF = 'time_off'

REFUSALS = {
    OUTSIDE_HOURS: "That is outside this professional's working hours.",
    TIME_OFF: 'This professional is off at that time.',
}


def opening_check(tenant, professional, since, until):
    """
    A function `(start, end) -> None | code` saying why the clock forbids a
    booking for `professional`: TIME_OFF when it touches an absence or a
    tenant-wide closure, OUTSIDE_HOURS when no stretch of their week holds it.

    A professional with NO weekly schedule at all has no hours to be outside
    of, so only time off applies to them: plenty of shops never fill the
    schedule in, and turning that blank into "closed every day" would stop
    them booking anything. The public page is stricter on purpose -- with no
    schedule it offers nothing (free_slots).

    The rows for [since, until) are read once, so a series of fifty asks two
    queries, not a hundred.

    ponytail: a booking must start and end on the same local day; one ending
    exactly at midnight reads as outside hours, since a shift cannot end at 24:00.
    """
    zone = ZoneInfo(tenant.timezone)
    week = defaultdict(list)
    for row in WorkSchedule.objects.filter(tenant=tenant, professional=professional):
        week[row.weekday].append((row.start_time, row.end_time))
    away = [
        (row.start, row.end)
        for row in TimeOff.objects.filter(tenant=tenant, start__lt=until, end__gt=since).filter(
            Q(professional=professional) | Q(professional__isnull=True)
        )
    ]

    def check(start, end):
        if any(a < end and b > start for a, b in away):
            return TIME_OFF
        if not week:
            return None
        local_start, local_end = start.astimezone(zone), end.astimezone(zone)
        if local_end.date() != local_start.date():
            return OUTSIDE_HOURS
        # The day's stretches merged, so 09-13 and 13-18 hold a 12:30 turn.
        merged = []
        for opens, closes in sorted(week[local_start.weekday()]):
            if merged and opens <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], closes)
            else:
                merged.append([opens, closes])
        begins, ends = local_start.time(), local_end.time()
        if any(opens <= begins and ends <= closes for opens, closes in merged):
            return None
        return OUTSIDE_HOURS

    return check
