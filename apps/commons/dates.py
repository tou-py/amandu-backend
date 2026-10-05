import calendar
from datetime import date


def add_months(day: date, months: int) -> date:
    """
    The same day `months` months later, clamped to a day that exists there: a
    period paid on the 31st ends on the 30th, not on the 1st of the month after.
    Written out rather than pulled from dateutil, which is not a dependency of
    this project and would be a whole package for these four lines.

    Lives in commons rather than beside its first caller because two things
    sell a month: the platform sells one to a tenant (tenancy admin) and a
    tenant sells plan periods to its clients (apps/scheduling/billing.py). The
    clamp is the part nobody gets right twice, so there is one of it.

    Always count from the ORIGINAL day, never chain: add_months(jan31, 2) is
    March 31st, while one_month_after(one_month_after(jan31)) drifts to the
    28th and stays there. Plan periods depend on exactly that difference.
    """
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def one_month_after(day: date) -> date:
    """The same day next month, clamped. See add_months."""
    return add_months(day, 1)
