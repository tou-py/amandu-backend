"""
The sweep. Three things come due on the same tick and share one run:

  * a tenant whose paid period ran out, which loses access;
  * a reminder of the appointment a professional is about to give;
  * a Notification row nobody has been told about yet -- today, a teammate
    cancelling a slot that was not theirs (AppointmentViewSet.cancel).

Kept in one command, under a name that only says "reminders", deliberately: the
cron entry lives in Dokploy's UI, not in this repo, and a second command means a
second entry somebody has to remember to create. One tick, one lock, one place
that can be misconfigured. `help` below says what it actually does.

WHERE THE SCHEDULE LIVES, because it is not in this repo and nothing here will
tell you it is missing: Dokploy > the API application > Schedule Jobs, running
`python manage.py send_reminders` inside the container every 5 minutes. That
cadence is what LOCK_TIMEOUT below is sized against, and a redeploy from scratch
does NOT recreate it -- an empty schedule looks exactly like a working one from
in here, silently. If reminders stop, check that entry exists before reading a
line of this file.

It is a sweep, not a queue: it asks the database what is due and sends it. There
is no job per appointment, on purpose:

  * the agenda lets anyone drag an appointment to a new time, and a scheduled job
    would have to be found and rewritten on every move;
  * a job that was due while the process was down is lost, whereas a sweep that
    missed its turn simply catches it on the next run.

Late is the failure mode, never silent and never twice. `reminder_sent_at` and
`Notification.pushed_at` are what make the second guarantee hold no matter how
often this runs.
"""
import json
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db.models import DateTimeField, ExpressionWrapper, F
from django.utils import timezone
from pywebpush import WebPushException, webpush

from apps.accounts.models import Notification
from apps.scheduling.models import Appointment
from apps.tenancy.models import Tenant

# RFC 8030 mandates 404 when the subscription has expired. Push services also
# answer 410 for one that was removed at the other end. Both mean the same thing
# here: that browser is gone and the row is now garbage.
DEAD_SUBSCRIPTION = (404, 410)

# A single unresponsive push endpoint used to hang this command forever (no
# request timeout was set), and the schedule keeps firing every 5
# minutes regardless -- each overlapping run held its own DB connection open,
# so delay crept up run after run until connections were exhausted and
# reminders stopped outright. The lock makes an overlapping tick a no-op
# instead of another process piling on top of the stuck one; the timeout
# below is what stops a run from getting stuck in the first place.
LOCK_KEY = 'send_reminders_lock'
LOCK_TIMEOUT = 240  # seconds -- expires before the next tick even if a run wedges
PUSH_TIMEOUT = 10  # seconds -- passed straight to requests.post via pywebpush


class Command(BaseCommand):
    help = (
        'Push appointment reminders that have come due, and any notification not '
        'yet delivered. Safe to run repeatedly.'
    )

    def handle(self, *args, **options):
        # First, and outside both the VAPID guard and the lock, on purpose:
        # collecting money must not depend on push being configured, and a single
        # idempotent UPDATE has nothing to serialise -- an overlapping tick
        # matches zero rows the second time.
        self._run_billing()

        if not (settings.VAPID_PRIVATE_KEY and settings.VAPID_SUBJECT):
            # Loud here rather than at boot: the API must serve an agenda without
            # push configured, but a cron that silently sends nothing every five
            # minutes is a feature that looks installed and is not.
            raise CommandError(
                'VAPID_PRIVATE_KEY and VAPID_SUBJECT are unset; push is not configured.'
            )

        if not cache.add(LOCK_KEY, True, LOCK_TIMEOUT):
            self.stdout.write('Another send_reminders run is still in progress; skipping.')
            return
        try:
            self._run()
            self._run_notifications()
        finally:
            cache.delete(LOCK_KEY)

    def _run_billing(self):
        """
        Cut off tenants whose paid period ended. `paid_until` is a date in the
        tenant's own calendar sense, so the comparison is against today, not now:
        a tenant paid through the 31st works all of the 31st.

        One direction only, and that asymmetry is the point. Suspending is safe
        to automate because SUSPENDED keeps the data intact and a payment undoes
        it. Reactivating is NOT: SUSPENDED also means "under review", so a rule
        that restored access on a future `paid_until` would quietly hand the
        platform back to a tenant we cut off for a reason that was never money.
        Lifting it stays a human decision, made in the admin by the same person
        who confirms the transfer.
        """
        # update() is deliberate over save(): it is one statement, it cannot race
        # with whatever else is editing the tenant, and it never loads a row it
        # only means to flag. auto_now does not fire on update(), hence the
        # explicit updated_at -- without it a suspension leaves no trace in time.
        suspended = Tenant.objects.filter(
            status=Tenant.Status.ACTIVE,
            # NULL never satisfies a comparison, which is exactly the wanted
            # behaviour: a tenant with no paid_until is never swept.
            paid_until__lt=timezone.localdate(),
        ).update(status=Tenant.Status.SUSPENDED, updated_at=timezone.now())

        if suspended:
            # Silent on the ordinary tick (this runs every 5 minutes), loud when
            # somebody actually lost access.
            self.stdout.write(f'{suspended} tenant(s) suspended for non-payment.')

    def _run(self):
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

    def _run_notifications(self):
        """
        The second half of the tick: rows written by the API that nobody has been
        told about outside the app.

        This is what makes a cancellation reach a closed phone. The bell in the
        app polls and covers the case where someone is already looking; a push is
        the only channel that reaches a device with no page open, which is the
        whole point of the service worker existing.
        """
        pending = (
            Notification.objects
            .filter(pushed_at__isnull=True)
            .select_related(
                'recipient__user', 'actor__user', 'appointment__service', 'appointment__tenant',
            )
            .prefetch_related('recipient__user__push_subscriptions')
        )

        due_count = pushed = 0
        for notification in pending:
            due_count += 1
            subscriptions = notification.recipient.user.push_subscriptions.all()
            if not subscriptions:
                # Same rule as a reminder with nobody to tell: NOT marked as
                # pushed, so allowing notifications later still delivers what is
                # waiting. `read_at` is the escape hatch for a row that is never
                # going to be pushed -- seeing it in the app retires it.
                continue

            payload = self.notification_payload(notification)
            delivered = [self.deliver(subscription, payload) for subscription in subscriptions]
            if any(delivered):
                notification.pushed_at = timezone.now()
                notification.save(update_fields=['pushed_at'])
                pushed += 1

        self.stdout.write(f'{due_count} notification(s) pending, {pushed} pushed.')

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
            # Per appointment, which is what sw.js's comment always claimed this
            # was and what the constant default never delivered: two different
            # slots due on the same sweep collapsed into one visible notification,
            # because a shared tag means "replace", not "stack".
            'tag': f'appointment-{appointment.pk}',
        }

    def notification_payload(self, notification):
        """
        A cancellation, said the way the person needs to hear it: whose slot,
        when it was, and who called it off. The appointment is SET_NULL, so every
        detail hangs off a row that may already be gone -- the title has to stand
        on its own without it.
        """
        appointment = notification.appointment
        actor = notification.actor.display_name() if notification.actor else 'Alguien del equipo'

        if appointment is None:
            body = f'{actor} canceló un turno tuyo.'
        else:
            local_start = appointment.start.astimezone(ZoneInfo(appointment.tenant.timezone))
            body = f'{actor} canceló {appointment.service.name} del {local_start:%d/%m a las %H:%M}.'

        return {
            'title': 'Turno cancelado',
            'body': body,
            'url': '/',
            # Per row: two cancellations must not replace each other, which is
            # exactly what a shared tag would do.
            'tag': f'notification-{notification.pk}',
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
                # Unset, this waited on the TCP connection forever: one endpoint
                # that accepts the connection and never answers used to freeze
                # the whole sweep, appointment after appointment, run after run.
                timeout=PUSH_TIMEOUT,
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
        except Exception as exc:
            # A timeout, DNS failure, or any other transport error raises here,
            # not as a WebPushException -- and used to propagate straight out
            # of this loop, aborting every appointment still waiting behind it.
            # This subscription stays intact and gets retried next run.
            self.stderr.write(
                f'Push to user {subscription.user_id} errored with {exc!r}.'
            )
            return False
