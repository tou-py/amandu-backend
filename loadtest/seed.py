"""
Fills the load-test database with businesses that look lived in: a team, a
catalogue, opening hours, hundreds of clients and six weeks of agenda around
today. Piped into `manage.py shell` (see compose.yaml), so nothing here ships
as a command in the prod image.

Every business is `load-<n>`; its people log in as owner@, pro1@..pro3@ and
desk@load-<n>.test with PASSWORD. Idempotent: a business already there is left
alone. LOAD_TENANTS sets how many (default 20), LOAD_HISTORY_DAYS how far
back their agenda goes (default 21).
"""
import os
import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, AppointmentClient, Category, Client, Service, WorkSchedule
from apps.tenancy.models import Tenant

PASSWORD = 'load12345'
TENANTS = int(os.environ.get('LOAD_TENANTS', 20))
CLIENTS = 300
DAYS = 21  # ahead of today
# Behind today: a year (365) is what shows the cost of billing's history.
HISTORY = int(os.environ.get('LOAD_HISTORY_DAYS', 21))
ZONE = ZoneInfo('America/Asuncion')
OPEN, CLOSE = time(9), time(18)
CATALOGUE = (
    ('Pelo', 'Corte', 30, 60_000),
    ('Pelo', 'Color', 90, 250_000),
    ('Pelo', 'Barba', 30, 40_000),
    ('Manos', 'Manicura', 45, 80_000),
)
User = get_user_model()
rng = random.Random(42)


def person(tenant, local, role, attends):
    user = User.objects.create_user(email=f'{local}@{tenant.slug}.test', password=PASSWORD, first_name=local.title())
    return Membership.objects.create(user=user, tenant=tenant, role=role, attends_appointments=attends)


@transaction.atomic
def business(n):
    slug = f'load-{n}'
    tenant = Tenant.objects.create(
        name=f'Load {n}', slug=slug, timezone=ZONE.key, country='PY',
        addon_cobros=True, addon_reservas=True, paid_until=timezone.localdate() + timedelta(days=90),
    )
    pros = [person(tenant, 'owner', Membership.Role.OWNER, True)]
    pros += [person(tenant, f'pro{i}', Membership.Role.STAFF, True) for i in (1, 2, 3)]
    person(tenant, 'desk', Membership.Role.COORDINATOR, False)

    WorkSchedule.objects.bulk_create(
        WorkSchedule(tenant=tenant, professional=pro, weekday=day, start_time=OPEN, end_time=CLOSE)
        for pro in pros for day in range(6)
    )
    categories = {name: Category.objects.create(tenant=tenant, name=name) for name in {c for c, *_ in CATALOGUE}}
    services = Service.objects.bulk_create(
        Service(tenant=tenant, category=categories[cat], name=name, duration=timedelta(minutes=minutes), price=price)
        for cat, name, minutes, price in CATALOGUE
    )
    clients = Client.objects.bulk_create(
        Client(tenant=tenant, name=f'Cliente {i}', phone=f'+595981{n:02d}{i:04d}') for i in range(CLIENTS)
    )

    # Each professional's days, walked slot by slot: a turn about two times in
    # three, so the agenda is busy but never overlaps.
    now = timezone.now()
    today = now.astimezone(ZONE).date()
    turns, links = [], []
    for pro in pros:
        for offset in range(-HISTORY, DAYS + 1):
            day = today + timedelta(days=offset)
            if day.weekday() == 6:
                continue
            cursor = datetime.combine(day, OPEN, tzinfo=ZONE)
            close = datetime.combine(day, CLOSE, tzinfo=ZONE)
            while cursor < close:
                service = rng.choice(services)
                end = cursor + service.duration
                if end > close:
                    break
                if rng.random() < 0.65:
                    # Past turns are closed, bar one in ten left for the backlog.
                    closed = end < now and rng.random() < 0.9
                    turn = Appointment(
                        tenant=tenant, professional=pro, service=service, start=cursor, end=end,
                        status=Appointment.Status.COMPLETED if closed else Appointment.Status.SCHEDULED,
                    )
                    turns.append(turn)
                    links.append(AppointmentClient(
                        appointment=turn, client=rng.choice(clients),
                        attendance=AppointmentClient.Attendance.ATTENDED if closed else AppointmentClient.Attendance.PENDING,
                    ))
                cursor = end
    Appointment.objects.bulk_create(turns)
    AppointmentClient.objects.bulk_create(links)
    return len(turns)


for n in range(1, TENANTS + 1):
    if Tenant.objects.filter(slug=f'load-{n}').exists():
        continue
    print(f'load-{n}: {business(n)} turns')
print(f'{TENANTS} businesses; log in as owner|pro1|pro2|pro3|desk@load-<n>.test / {PASSWORD}')
