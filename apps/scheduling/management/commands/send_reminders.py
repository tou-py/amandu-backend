"""
Push each professional a reminder of the appointment they are about to give.

Run from cron -- Dokploy's Schedule Jobs execs it inside the API container --
every few minutes. It is a sweep, not a queue: it asks the database which
appointments are due to be announced and announces them. There is no job per
appointment, on purpose:

  * the agenda lets anyone drag an appointment to a new time, and a scheduled job
    would have to be found and rewritten on every move;
  * a job that was due while the process was down is lost, whereas a sweep that
    missed its turn simply catches the appointment on the next run.

Late is the failure mode, never silent and never twice. `reminder_sent_at` is
what makes the second guarantee hold no matter how often this runs.
"""
import json
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import DateTimeField, ExpressionWrapper, F
from django.utils import timezone
from pywebpush import WebPushException, webpush

from apps.scheduling.models import Appointment

# RFC 8030 mandates 404 when the subscription has expired. Push services also
# answer 410 for one that was removed at the other end. Both mean the same thing
# here: that browser is gone and the row is now garbage.
DEAD_SUBSCRIPTION = (404, 410)


class Command(BaseCommand):
    help = 'Push appointment reminders that have come due. Safe to run repeatedly.'

    def handle(self, *args, **options):
        if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_SUBJECT):
            # Loud here rather than at boot: the API must serve an agenda without
            # push configured, but a cron that silently sends nothing every five
            # minutes is a feature that looks installed and is not.
            raise CommandError(
                'VAPID_PRIVATE_KEY and VAPID_SUBJECT are unset; push is not configured.'
            )

        now = timezone.now()
        due = (
            Appointment.objects.filter(
                status=Appointment.Status.SCHEDULED,
                reminder_sent_at__isnull=True,
                # Never announce something that already started. This is also what
                # lets the lower bound stay open: an appointment whose moment
                # passed while the cron was down still gets its late reminder, and
                # one whose time has come and gone drops out on its own.
                start__gt=now,
            )
            .annotate(
                # Subtracted by Postgres as timestamptz - interval. Doing it here
                # rather than in Python is what keeps this ONE query when every
                # user has a different lead time.
                notify_at=ExpressionWrapper(
                    F('start') - F('professional__user__reminder_lead'),
                    output_field=DateTimeField(),
                )
            )
            .filter(notify_at__lte=now)
            .select_related('professional__user', 'service', 'tenant')
            .prefetch_related('clients', 'professional__user__push_subscriptions')
        )

        due_count = reminded = 0
        for appointment in due:
            due_count += 1
            subscriptions = appointment.professional.user.push_subscriptions.all()
            if not subscriptions:
                # Nobody to tell. Deliberately NOT marked as sent: if this person
                # allows notifications ten minutes from now, they should still get
                # the reminder for a turn that has not happened yet.
                continue

            payload = self.payload(appointment)
            # A list, not a generator inside any(): every device this person
            # registered must be tried, both so the reminder reaches the phone
            # AND the laptop, and so a dead subscription behind a live one still
            # gets pruned. any() over a generator would stop at the first hit.
            delivered = [self.deliver(subscription, payload) for subscription in subscriptions]
            if any(delivered):
                appointment.reminder_sent_at = timezone.now()
                appointment.save(update_fields=['reminder_sent_at', 'updated_at'])
                reminded += 1

        self.stdout.write(f'{due_count} due, {reminded} reminded.')

    def payload(self, appointment):
        """
        What the service worker will show. Times are rendered in the TENANT's zone,
        not the server's and not the reader's: "10:30" has to mean the 10:30
        written in the agenda, whatever timezone the phone is in.
        """
        local_start = appointment.start.astimezone(ZoneInfo(appointment.tenant.timezone))
        clients = ', '.join(client.name for client in appointment.clients.all())
        return {
            'title': f'Turno a las {local_start:%H:%M}',
            'body': f'{appointment.service.name}' + (f' · {clients}' if clients else ''),
            # The service worker opens this on click. Relative so it works on
            # whatever origin the app is deployed to.
            'url': '/',
        }

    def deliver(self, subscription, payload):
        """True when the push service accepted the message. Prunes the
        subscription when it answers that the browser is gone."""
        try:
            webpush(
                subscription_info=subscription.subscription_info,
                data=json.dumps(payload),
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={'sub': settings.VAPID_SUBJECT},
                # WNS (Edge/Windows) rejects TTL=0 with 400; FCM and Mozilla accept it.
                ttl=1800,
            )
            return True
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in DEAD_SUBSCRIPTION:
                subscription.delete()
                # Not a failure worth reporting: this is the documented way a
                # subscription ends, and deleting it IS handling it.
                return False
            # The endpoint is a secret, so it never reaches this line.
            self.stderr.write(
                f'Push to user {subscription.user_id} failed with {status or exc}.'
            )
            return False
