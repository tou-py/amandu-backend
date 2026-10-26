"""
Dev-only seeder: ONE tenant built to be looked at -- a plausible Paraguayan hair
and beauty salon for screenshots, screen recordings and live demos.

`seed_demo` is the minimum to log into; `seed_sandbox` exists to break the
front end. Neither looks true on the day of a capture: their dates are fixed on
the first run, so two weeks later the current week is empty and every past turn
is "sin cerrar". This one fixes that by keeping two kinds of rows:

- Stable rows (tenant, users, memberships, working hours, categories, services,
  clients, plans, subscriptions) are fetched by natural key, so re-running
  never duplicates them.
- Dated rows (appointments, their attendees, the cash book, the public-booking
  request) belong to the showcase tenant only and are deleted and rebuilt on
  every run, from day offsets relative to today in the tenant's timezone.

So run it on the day of a capture: the week is full around today, the recent
past is closed and charged, and Para hacer and Por cobrar have a believable
handful of items. No other tenant is ever touched.

The business, its people and its clients are invented. Never present them as a
real customer.
"""
from datetime import datetime, time, timedelta
from itertools import cycle
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership
from apps.commons.dates import add_months
from apps.scheduling import billing
from apps.scheduling.models import (
    Appointment, AppointmentClient, Category, Client, Plan, Service, Subscription, WorkSchedule,
)
from apps.tenancy.models import Tenant

User = get_user_model()

PASSWORD = 'demo12345'
SLUG = 'showcase'

# The window rebuilt on every run, in days from today.
FIRST_DAY, LAST_DAY = -14, 7

OWNER = Membership.Role.OWNER
COORDINATOR = Membership.Role.COORDINATOR
STAFF = Membership.Role.STAFF

# (email, role, display name, opens on Sundays, the services they rotate through)
# No menu means the person does not attend: the front desk books for everyone.
PEOPLE = [
    ('lorena@magnolia.test', OWNER, 'Lorena Acosta', True,
     ['Coloración', 'Corte de dama', 'Mechas', 'Brushing']),
    ('gabriela@magnolia.test', COORDINATOR, 'Gabriela Duarte', False, None),
    ('diego@magnolia.test', STAFF, 'Diego Ramírez', False,
     ['Corte de caballero', 'Corte y barba', 'Corte de dama', 'Corte de caballero']),
    ('fatima@magnolia.test', STAFF, 'Fátima Giménez', True,
     ['Manicura', 'Esmaltado semipermanente', 'Pedicura', 'Manicura']),
    ('noelia@magnolia.test', STAFF, 'Noelia Ortiz', False,
     ['Brushing', 'Tratamiento capilar', 'Peinado de fiesta', 'Corte de dama']),
]
LOGINS = {OWNER: PEOPLE[0][0], COORDINATOR: PEOPLE[1][0], STAFF: PEOPLE[2][0]}

# Open every day, Sunday included, so a capture made on any day has a "today".
# Long hours for the same reason: a demo at 18:00 still has turns after it.
WEEKDAY_HOURS = (time(8, 0), time(20, 0))
SATURDAY_HOURS = (time(8, 0), time(18, 0))
SUNDAY_HOURS = (time(9, 0), time(17, 0))

# (name, minutes, price in guaraníes, category)
SERVICES = [
    ('Corte de dama', 45, 80_000, 'Peluquería'),
    ('Corte de caballero', 30, 50_000, 'Peluquería'),
    ('Corte y barba', 45, 70_000, 'Peluquería'),
    ('Brushing', 30, 60_000, 'Peluquería'),
    ('Tratamiento capilar', 60, 120_000, 'Peluquería'),
    ('Peinado de fiesta', 60, 150_000, 'Peluquería'),
    ('Coloración', 120, 280_000, 'Color'),
    ('Mechas', 150, 380_000, 'Color'),
    ('Manicura', 45, 50_000, 'Manos y pies'),
    ('Esmaltado semipermanente', 60, 90_000, 'Manos y pies'),
    ('Pedicura', 60, 70_000, 'Manos y pies'),
]
# The one group service: a bride and two friends in one slot, which is what the
# roster in the turn dialog is for.
GROUP_SERVICE, GROUP_SIZE = 'Peinado de fiesta', 3

# (name, price, sessions per period or None for unlimited, categories)
PLANS = [
    ('Brushing semanal', 200_000, 4, ['Peluquería']),
    ('Manos impecables', 180_000, 4, ['Manos y pies']),
    ('Belleza libre', 450_000, None, []),
]

# (name, phone, email, notes). Ordinary local names, Paraguayan mobiles only.
CLIENTS = [
    ('María José González', '+595981452310', 'mariajose.gonzalez@correo.test', ''),
    ('Carmen Benítez', '+595982317645', '', 'Prefiere turnos a la mañana.'),
    ('Mirna Villalba', '+595971208833', 'mirna.villalba@correo.test', ''),
    ('Andrea Martínez', '+595983664120', '', ''),
    ('Karina López', '+595991540276', 'karina.lopez@correo.test', 'Alérgica a la tintura con amoníaco.'),
    ('Rocío Cáceres', '+595984102957', '', ''),
    ('Natalia Rojas', '+595972885014', '', ''),
    ('Sonia Báez', '+595981773402', '', 'Viene con su hija.'),
    ('Diana Fernández', '+595985219760', 'diana.fernandez@correo.test', ''),
    ('Fabiola Núñez', '+595992366185', '', ''),
    ('Romina Cabrera', '+595976410528', '', ''),
    ('Patricia Escobar', '+595982951374', '', ''),
    ('Mónica Ayala', '+595981630947', 'monica.ayala@correo.test', ''),
    ('Gladys Franco', '+595983427591', '', 'Cabello muy fino, usar productos suaves.'),
    ('Nilda Cardozo', '+595971856203', '', ''),
    ('Jazmín Aquino', '+595994172668', 'jazmin.aquino@correo.test', ''),
    ('Belén Galeano', '+595985703419', '', ''),
    ('Leticia Recalde', '+595981294856', '', ''),
    ('Cynthia Medina', '+595982580713', '', 'Paga siempre por transferencia.'),
    ('Adriana Ojeda', '+595973641092', '', ''),
    ('Celeste Espínola', '+595991827354', 'celeste.espinola@correo.test', ''),
    ('Liliana Sosa', '+595984368120', '', ''),
    ('Verónica Insfrán', '+595981915637', '', ''),
    ('Laura Vera', '+595986247081', '', ''),
    ('Marcelo Duarte', '+595982736409', '', ''),
    ('Hugo Giménez', '+595971390562', '', ''),
    ('Óscar Ramírez', '+595983052847', '', ''),
    ('Javier Acosta', '+595992614730', 'javier.acosta@correo.test', ''),
]
# Only ever seen on the public-booking request: a stranger asking for a slot.
REQUEST_CLIENT = ('Camila Ruiz Díaz', '+595985471396', 'camila.ruizdiaz@correo.test', '')

UP_TO_DATE, DUE_SOON = 'up_to_date', 'due_soon'
# (client name, plan name, standing)
SUBSCRIPTIONS = [
    ('María José González', 'Brushing semanal', UP_TO_DATE),
    ('Carmen Benítez', 'Manos impecables', UP_TO_DATE),
    ('Mirna Villalba', 'Brushing semanal', DUE_SOON),
]

# The past turns that carry a story, as (what, latest day offset it may sit on).
# Two left unclosed and two left owing make four clients in Por cobrar; the
# no-show and the cancellation make the timeline look lived-in.
ROLES = [
    ('unclosed', -1), ('unclosed', -1),
    ('owed', -2), ('owed', -4),
    ('no_show', -3), ('cancelled', -5),
]


class Command(BaseCommand):
    help = 'Seed the showcase tenant for screenshots and demos (dev only). Re-dates on every run.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Run even when DEBUG is False. This creates users with a known '
                 'password -- never point it at real data.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        # A seeder that mints known-password users must fail closed outside dev.
        if not settings.DEBUG and not options['force']:
            raise CommandError(
                'Refusing to seed with DEBUG=False. Pass --force only against a '
                'throwaway database.'
            )

        tenant = self.seed_tenant()
        front_desk, professionals = self.seed_people(tenant)
        services = self.seed_catalogue(tenant)
        clients = self.seed_clients(tenant)

        tz = ZoneInfo(tenant.timezone)
        now = timezone.now()
        today = now.astimezone(tz).date()

        # Everything dated goes, scoped to this tenant only. The cash book first:
        # its link to an appointment is SET_NULL, so deleting turns first would
        # leave orphaned charges behind instead of removing them.
        CashEntry.objects.filter(tenant=tenant).delete()
        Appointment.objects.filter(tenant=tenant).delete()  # attendees CASCADE

        subscribed = self.seed_plans(tenant, clients, front_desk, today)
        turns = self.seed_turns(
            tenant, tz, now, today, front_desk, professionals, services, clients, subscribed,
        )
        self.charge_turns(tenant, front_desk, turns)
        self.report(tenant, turns, now)

    def seed_tenant(self):
        # update_or_create, not get_or_create: whatever a demo toggled (public
        # booking off, a renamed business) is put back on the next run.
        tenant, _ = Tenant.objects.update_or_create(
            slug=SLUG,
            defaults={
                'name': 'Salón Magnolia',
                'timezone': 'America/Asuncion',
                'country': 'PY',
                'status': Tenant.Status.ACTIVE,
                'public_booking': True,
                'phone': '+595981300400',
                # Cobros is what makes Planes, Por cobrar and the charges show.
                'addon_cobros': True,
                'plan_period_start': Tenant.PlanPeriodStart.JOIN_DAY,
            },
        )
        return tenant

    def seed_people(self, tenant):
        front_desk = None
        professionals = []
        for email, role, display, sundays, menu in PEOPLE:
            first, _, last = display.partition(' ')
            user, _ = User.objects.get_or_create(
                email=email, defaults={'first_name': first, 'last_name': last}
            )
            # Reset every run so the password is always the documented one.
            user.set_password(PASSWORD)
            user.save(update_fields=['password'])
            membership, _ = Membership.objects.get_or_create(
                user=user,
                tenant=tenant,
                defaults={'role': role, 'attends_appointments': menu is not None},
            )
            if menu is None:
                front_desk = membership
                continue

            hours = {weekday: WEEKDAY_HOURS for weekday in range(5)}
            hours[5] = SATURDAY_HOURS
            if sundays:
                hours[6] = SUNDAY_HOURS
            for weekday, (start, end) in hours.items():
                WorkSchedule.objects.get_or_create(
                    tenant=tenant,
                    professional=membership,
                    weekday=weekday,
                    start_time=start,
                    defaults={'end_time': end},
                )
            professionals.append((membership, menu, hours))
        return front_desk, professionals

    def seed_catalogue(self, tenant):
        services = {}
        for name, minutes, price, label in SERVICES:
            category, _ = Category.objects.get_or_create(tenant=tenant, name=label)
            services[name], _ = Service.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={
                    'duration': timedelta(minutes=minutes),
                    'price': price,
                    'category': category,
                },
            )
        return services

    def seed_clients(self, tenant):
        clients = {}
        for name, phone, email, notes in [*CLIENTS, REQUEST_CLIENT]:
            # Keyed on the phone, the tenant's own identity rule for a client.
            clients[name], _ = Client.objects.get_or_create(
                tenant=tenant,
                phone=phone,
                defaults={'name': name, 'email': email, 'notes': notes},
            )
        return clients

    def seed_plans(self, tenant, clients, front_desk, today):
        """
        The catalogue, three subscriptions and the period payments behind them.

        The subscription rows are stable but their start dates are not: whether
        a plan reads "al día" or "vence pronto" is a matter of where today falls
        in its period, so every run re-anchors them to today and pays the
        periods the standing calls for.
        """
        plans = {}
        for name, price, sessions, labels in PLANS:
            plans[name], _ = Plan.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={'price': price, 'sessions_per_period': sessions},
            )
            plans[name].categories.set(Category.objects.filter(tenant=tenant, name__in=labels))

        subscribed = set()
        entries = []
        for client_name, plan_name, standing in SUBSCRIPTIONS:
            client = clients[client_name]
            if standing == UP_TO_DATE:
                # Joined about six weeks ago; the current period started 12 days
                # ago and is paid, like the one before it.
                anchor = today - timedelta(days=12)
            else:
                # The current period started just long enough ago to fall due
                # within the next few days: "vence pronto", not yet overdue.
                anchor = today - timedelta(days=tenant.plan_grace_days - billing.DUE_SOON_DAYS)
            subscription, _ = Subscription.objects.update_or_create(
                tenant=tenant,
                client=client,
                defaults={
                    'plan': plans[plan_name],
                    'start_date': add_months(anchor, -1),
                    'end_date': None,
                    'price_override': None,
                    'anchor_day': None,
                },
            )
            current = billing.period_index(subscription, today)
            paid = current + 1 if standing == UP_TO_DATE else current
            for k in range(paid):
                period = billing.make_period(subscription, k, today)
                entries.append(CashEntry(
                    tenant=tenant,
                    kind=CashEntry.Kind.INCOME,
                    amount=period.amount,
                    occurred_on=period.start,
                    concept=billing.concept_for_period(client, period),
                    payment_method=CashEntry.PaymentMethod.TRANSFER,
                    client=client,
                    subscription=subscription,
                    period=period.start,
                    recorded_by=front_desk.user,
                ))
            subscribed.add(client)
        CashEntry.objects.bulk_create(entries)
        return subscribed

    def seed_turns(self, tenant, tz, now, today, front_desk, professionals, services, clients, subscribed):
        """Lay out the window, give a few past turns their story, and write it."""
        regulars = cycle(clients[name] for name, *_ in CLIENTS)
        turns = []
        for offset in range(FIRST_DAY, LAST_DAY + 1):
            day = today + timedelta(days=offset)
            for p, (professional, menu, hours) in enumerate(professionals):
                if day.weekday() not in hours:
                    continue
                opens, closes = (
                    datetime.combine(day, moment, tzinfo=tz) for moment in hours[day.weekday()]
                )
                span = (closes - opens) // timedelta(minutes=30)
                # Two to four turns each, spread over the whole day rather than
                # packed into the morning, so "now" has turns on both sides.
                count = 2 + (offset + p) % 3
                cursor = opens
                for i in range(count):
                    service = services[menu[(offset + 2 * p + i) % len(menu)]]
                    aim = opens + timedelta(minutes=30 * (i * span // count + (offset + i) % 2))
                    start = max(aim, cursor)
                    end = start + service.duration
                    if end > closes:
                        break
                    cursor = end
                    # ponytail: a turn running right now is left out rather than
                    # half-closed; the agenda shows a free chair at the current hour.
                    if start < now < end:
                        continue
                    size = GROUP_SIZE if service.name == GROUP_SERVICE else 1
                    past = end <= now
                    turns.append({
                        'appointment': Appointment(
                            tenant=tenant,
                            professional=professional,
                            service=service,
                            start=start,
                            end=end,
                            status=Appointment.Status.COMPLETED if past else Appointment.Status.SCHEDULED,
                            created_by=front_desk,
                        ),
                        'day': day,
                        'clients': [next(regulars) for _ in range(size)],
                        'attendance': (
                            AppointmentClient.Attendance.ATTENDED if past
                            else AppointmentClient.Attendance.PENDING
                        ),
                        'role': None,
                    })

        self.assign_roles(turns, today, subscribed, now, clients)

        Appointment.objects.bulk_create([turn['appointment'] for turn in turns])
        AppointmentClient.objects.bulk_create(
            AppointmentClient(
                appointment=turn['appointment'], client=client, attendance=turn['attendance'],
            )
            for turn in turns
            for client in turn['clients']
        )
        return turns

    def assign_roles(self, turns, today, subscribed, now, clients):
        """
        Pick the past turns that are not tidy. Only single-client turns of
        clients without a plan, and a different client for every debt, so the
        number of rows in Por cobrar is exactly the number of debts made here.
        """
        earlier = sorted(
            (
                turn for turn in turns
                if turn['day'] < today
                and len(turn['clients']) == 1
                and turn['clients'][0] not in subscribed
            ),
            key=lambda turn: turn['appointment'].start,
            reverse=True,
        )
        debtors = set()
        for role, latest in ROLES:
            pick = next(
                (
                    turn for turn in earlier
                    if turn['role'] is None
                    and turn['day'] <= today + timedelta(days=latest)
                    and not (role in ('unclosed', 'owed') and turn['clients'][0] in debtors)
                ),
                None,
            )
            if pick is None:
                continue
            pick['role'] = role
            appointment = pick['appointment']
            if role in ('unclosed', 'owed'):
                debtors.add(pick['clients'][0])
            if role == 'unclosed':
                # Nobody marked it: still "scheduled" after it happened.
                appointment.status = Appointment.Status.SCHEDULED
                pick['attendance'] = AppointmentClient.Attendance.PENDING
            elif role == 'no_show':
                pick['attendance'] = AppointmentClient.Attendance.NO_SHOW
            elif role == 'cancelled':
                appointment.status = Appointment.Status.CANCELLED
                appointment.cancelled_at = appointment.start - timedelta(hours=20)
                appointment.cancellation_reason = 'Avisó por WhatsApp que no podía venir.'
                pick['attendance'] = AppointmentClient.Attendance.PENDING
            # 'owed' stays completed and attended; charge_turns leaves it unpaid.

        # Exactly one request off the public page, the first single-client turn
        # after today, asked for by someone the salon has never seen.
        request = next(
            turn for turn in turns if turn['day'] > today and len(turn['clients']) == 1
        )
        request['role'] = 'request'
        request['clients'] = [clients[REQUEST_CLIENT[0]]]
        request['appointment'].status = Appointment.Status.PENDING
        request['appointment'].source = Appointment.Source.PUBLIC
        request['appointment'].created_by = None
        request['appointment'].notes = 'Reservado desde la página pública.'

    def charge_turns(self, tenant, front_desk, turns):
        """
        Charge every closed, attended turn for whatever billing says it costs --
        the same answer the turn dialog gives, so a plan-covered turn is not
        charged and an extra past the quota is. The 'owed' ones are left unpaid.
        """
        by_pk = {turn['appointment'].pk: turn for turn in turns}
        methods = cycle(CashEntry.PaymentMethod.values[:3])  # cash, card, transfer
        entries = []
        prefetched = Client.objects.for_tenant(tenant).prefetch_related(*billing.client_prefetches())
        for client in prefetched:
            for link in client.appointment_links.all():
                turn = by_pk[link.appointment_id]
                if (
                    turn['role'] == 'owed'
                    or link.appointment.status != Appointment.Status.COMPLETED
                    or link.attendance != AppointmentClient.Attendance.ATTENDED
                ):
                    continue
                state = billing.attendee_billing(link)
                if state['state'] not in (billing.State.CHARGE, billing.State.EXTRA):
                    continue
                entries.append(CashEntry(
                    tenant=tenant,
                    kind=CashEntry.Kind.INCOME,
                    amount=state['amount'],
                    occurred_on=turn['day'],
                    concept=billing.concept_for_turn(client, turn['appointment']),
                    payment_method=next(methods),
                    appointment=turn['appointment'],
                    client=client,
                    recorded_by=front_desk.user,
                ))
        CashEntry.objects.bulk_create(entries)

    def report(self, tenant, turns, now):
        today = [turn for turn in turns if turn['day'] == now.astimezone(ZoneInfo(tenant.timezone)).date()]
        roles = [turn['role'] for turn in turns if turn['role']]
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Seeded "{tenant.name}" -- slug {tenant.slug}, X-Tenant-ID: {tenant.id}. '
            f'Password: {PASSWORD}'
        ))
        for role, email in LOGINS.items():
            self.stdout.write(f'  {role.label:<12} {email}')
        self.stdout.write(
            f'  {len(turns)} turns from {FIRST_DAY} to +{LAST_DAY} days, {len(today)} today '
            f'({sum(turn["appointment"].start < now for turn in today)} before now); '
            f'{roles.count("unclosed")} unclosed, {roles.count("owed")} owed, '
            f'{roles.count("request")} pending request.'
        )
        self.stdout.write(f'  Public page: /reservar/{tenant.slug}/')
