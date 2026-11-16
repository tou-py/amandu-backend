"""
The recurrence rule on its own: which local days a series lands on.

No database and no request -- occurrence_dates is pure date arithmetic, and it
is where every off-by-one lives. The API-level behaviour is in
test_appointment_series_api.py.
"""

from datetime import date

from apps.scheduling.models import MAX_OCCURRENCES, AppointmentSeries


def rule(**kwargs):
    kwargs.setdefault('frequency', AppointmentSeries.Frequency.WEEKLY)
    kwargs.setdefault('weekdays', [])
    return AppointmentSeries(**kwargs)


def test_weekly_repeats_the_starting_weekday_when_none_is_chosen():
    """Picking a date and saying "every week" means that day, every week."""
    days = rule(until=date(2026, 3, 24)).occurrence_dates(date(2026, 3, 3))

    assert days == [date(2026, 3, 3), date(2026, 3, 10), date(2026, 3, 17), date(2026, 3, 24)]


def test_until_is_inclusive_and_stops_there():
    days = rule(until=date(2026, 3, 17)).occurrence_dates(date(2026, 3, 3))

    assert days[-1] == date(2026, 3, 17)


def test_two_weekdays_are_one_arrangement():
    """Mondays and Wednesdays: one decision, one series, both days."""
    # 2026-03-02 is a Monday.
    days = rule(weekdays=[0, 2], until=date(2026, 3, 15)).occurrence_dates(date(2026, 3, 2))

    assert days == [
        date(2026, 3, 2), date(2026, 3, 4),
        date(2026, 3, 9), date(2026, 3, 11),
    ]


def test_the_first_week_is_partial():
    """A Wednesday start does not book the Monday that already happened."""
    days = rule(weekdays=[0, 2], until=date(2026, 3, 9)).occurrence_dates(date(2026, 3, 4))

    assert days == [date(2026, 3, 4), date(2026, 3, 9)]


def test_an_interval_counts_whole_weeks_from_the_starting_week():
    """Every three weeks is the salon's colour root."""
    days = rule(interval=3, until=date(2026, 4, 30)).occurrence_dates(date(2026, 3, 4))

    assert days == [date(2026, 3, 4), date(2026, 3, 25), date(2026, 4, 15)]


def test_monthly_keeps_the_same_day_of_the_month():
    days = rule(
        frequency=AppointmentSeries.Frequency.MONTHLY,
        interval=6,
        until=date(2027, 12, 31),
    ).occurrence_dates(date(2026, 3, 15))

    assert days == [date(2026, 3, 15), date(2026, 9, 15), date(2027, 3, 15), date(2027, 9, 15)]


def test_a_month_too_short_is_skipped_not_clamped():
    """
    Somebody booked the 31st. The 28th of February is a different day, and
    quietly moving them there is a decision nobody made.
    """
    days = rule(
        frequency=AppointmentSeries.Frequency.MONTHLY,
        until=date(2026, 4, 30),
    ).occurrence_dates(date(2026, 1, 31))

    assert days == [date(2026, 1, 31), date(2026, 3, 31)]


def test_monthly_crosses_the_year_boundary():
    days = rule(
        frequency=AppointmentSeries.Frequency.MONTHLY,
        interval=2,
        until=date(2027, 2, 28),
    ).occurrence_dates(date(2026, 11, 10))

    assert days == [date(2026, 11, 10), date(2027, 1, 10)]


def test_generation_is_capped_however_far_until_reaches():
    """The ceiling that stops a typo in `until` from inserting forever."""
    days = rule(until=date(2099, 1, 1)).occurrence_dates(date(2026, 1, 1))

    assert len(days) == MAX_OCCURRENCES


def test_a_series_ending_before_it_starts_produces_nothing():
    assert rule(until=date(2026, 3, 1)).occurrence_dates(date(2026, 3, 3)) == []
