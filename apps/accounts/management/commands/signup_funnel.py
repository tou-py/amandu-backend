"""
How many businesses signed themselves up and how far they got, from the API's own
rows: no third party, and nothing for anyone to consent to.

ponytail: signups nobody confirmed are swept after a week (signup.sweep_unverified),
so "unverified" only counts the last seven days. Store a counter if the drop-off
before verifying ever matters more than that.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Exists, F, OuterRef
from django.utils import timezone

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment
from apps.tenancy.models import Tenant


class Command(BaseCommand):
    help = 'Self-served businesses by stage: unverified, verified, first turn, paying.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='Businesses created in the last N days.')

    def handle(self, *args, days, **options):
        recent = Tenant.objects.filter(created_at__gte=timezone.now() - timedelta(days=days))
        unverified = recent.filter(
            Exists(Membership.objects.filter(tenant=OuterRef('pk'), user__email_verified_at__isnull=True)),
            trial_until__isnull=True,
        )
        verified = recent.filter(trial_until__isnull=False)
        rows = (
            ('Unverified', unverified.count()),
            ('Verified', verified.count()),
            ('First turn', verified.filter(Exists(Appointment.objects.filter(tenant=OuterRef('pk')))).count()),
            ('Paying', verified.filter(paid_until__gt=F('trial_until')).count()),
        )
        self.stdout.write(f'Businesses created in the last {days} days')
        for label, count in rows:
            self.stdout.write(f'{label:<12}{count}')
