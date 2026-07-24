"""
Dev-only seeder: builds one demo tenant with users, a service catalog, clients
and a couple of appointments, so a fresh database is something you can log into
and click around instead of an empty admin.

Not a production tool. Production has no data to seed: roles are enum choices,
tenants are provisioned by hand, and the only bootstrap prod needs is a
superuser -- which Django already gives you via
`createsuperuser --noinput` with DJANGO_SUPERUSER_* env vars.

Idempotent: every row is fetched by its natural key with get_or_create, so
running it twice changes nothing and never trips a unique or exclusion
constraint. Safe to re-run.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Category, Client, Service
from apps.tenancy.models import Tenant

User = get_user_model()

# Same password for every demo user: this database is throwaway and the whole
# point is to log in without hunting for credentials. Printed at the end.
DEMO_PASSWORD = 'demo12345'


class Command(BaseCommand):
    help = 'Seed a demo tenant (dev only). Idempotent.'

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

        tenant, _ = Tenant.objects.get_or_create(
            slug='demo',
            defaults={
                'name': 'Demo Clinic',
                'timezone': 'America/Argentina/Buenos_Aires',
                'country': 'AR',
            },
        )

        memberships = {}
        for email, role in (
            ('owner@demo.test', Membership.Role.OWNER),
            ('admin@demo.test', Membership.Role.ADMIN),
            ('staff@demo.test', Membership.Role.STAFF),
        ):
            user, _ = User.objects.get_or_create(email=email)
            # Reset every run so the dev always knows the password, even if a
            # previous seed used a different one.
            user.set_password(DEMO_PASSWORD)
            user.save(update_fields=['password'])
            membership, _ = Membership.objects.get_or_create(
                user=user,
                tenant=tenant,
                defaults={
                    'role': role,
                    # Only staff show up in the agenda as someone who attends.
                    'attends_appointments': role == Membership.Role.STAFF,
                },
            )
            memberships[role] = membership

        professional = memberships[Membership.Role.STAFF]

        haircuts, _ = Category.objects.get_or_create(tenant=tenant, name='Haircuts')
        service, _ = Service.objects.get_or_create(
            tenant=tenant,
            name='Haircut',
            defaults={'duration': timedelta(minutes=30), 'category': haircuts},
        )

        clients = []
        for name, phone in (
            ('Ada Lovelace', '+5491111111111'),
            ('Alan Turing', '+5491122222222'),
        ):
            client, _ = Client.objects.get_or_create(
                tenant=tenant, phone=phone, defaults={'name': name}
            )
            clients.append(client)

        # Appointments anchored to today at 10:00 in the tenant's own timezone.
        # get_or_create keyed on (professional, start) is what keeps a re-run from
        # inserting a second overlapping row and hitting no_overlap_per_professional.
        tz = ZoneInfo(tenant.timezone)
        today = timezone.now().astimezone(tz).date()
        first_slot = datetime.combine(today, time(10, 0), tzinfo=tz)
        for i, client in enumerate(clients):
            start = first_slot + timedelta(hours=i)
            appointment, created = Appointment.objects.get_or_create(
                professional=professional,
                start=start,
                defaults={
                    'tenant': tenant,
                    'service': service,
                    'end': start + service.duration,
                },
            )
            if created:
                appointment.clients.add(client)

        self.stdout.write(self.style.SUCCESS(
            f'Seeded tenant "{tenant.slug}" — log in as owner@demo.test / '
            f'admin@demo.test / staff@demo.test (password: {DEMO_PASSWORD}). '
            f'Send X-Tenant-ID: {tenant.id}.'
        ))
