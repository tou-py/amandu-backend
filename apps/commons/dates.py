import calendar
from datetime import date


def one_month_after(day: date) -> date:
    """
    The same day next month, clamped to a day that exists there: a period paid on
    the 31st ends on the 30th, not on the 1st of the month after. Written out
    rather than pulled from dateutil, which is not a dependency of this project
    and would be a whole package for these four lines.

    Lives in commons rather than beside its first caller because two things now
    sell a month: the platform sells one to a tenant (tenancy admin) and a tenant
    sells one to a client (ClientViewSet.register_payment). The clamp is the part
    nobody gets right twice, so there is one of it.
    """
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))
