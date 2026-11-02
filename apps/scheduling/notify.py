"""
Every notice Kyo gives, decided in one place.

Something happened -- a turno was booked, moved, cancelled, asked for off the
public page, left unanswered; the morning's money; Kyo's own bill -- and the
caller says only WHAT happened (`notify(EVENT, ...)`). Who hears about it, and
through which channel, is the table below and nowhere else. Nothing outside
this module creates a Notification or an OutboundMessage.

Two channels:

  * push: a Notification row for a team member. The bell shows it at once; the
    sweep (send_reminders) pushes it to their devices.
  * WhatsApp from Kyo's number: to the client of a public request who ticked
    the box -- the receipt and the expiry only; the shop's answer goes from its
    own WhatsApp. Filed here as an OutboundMessage. Never to the team: they have
    push.

One rule for everyone: nobody is told about what they did themselves.

Called inside the transaction that made the change, so a turno and its notices
exist together or not at all.

The professional's reminder before a turno is not here: it is not news about
a change, it is computed from the clock and pushed by the sweep directly
(send_reminders._run), stamped on the appointment so it goes once.
"""

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import OutboundMessage

Verb = Notification.Verb

# A teammate changed somebody's diary.
BOOKED = 'booked'
MOVED = 'moved'
CHANGED = 'changed'
REASSIGNED = 'reassigned'
CANCELLED = 'cancelled'
# A request off the public page, and what became of it.
REQUESTED = 'requested'
ACCEPTED = 'accepted'
REJECTED = 'rejected'
# Nobody answered before the hour came (the sweep turns it down).
EXPIRED = 'expired'
# Nobody answered for a while (the sweep puts it to the managers).
WAITING = 'waiting'
# About the business, not a turno.
DIGEST = 'digest'
BILLING_DUE = 'billing_due'

# Audiences.
PROFESSIONAL = 'professional'  # whose diary the turno is in
PREVIOUS = 'previous'  # whose diary it was taken from
MANAGERS = 'managers'  # owner, admins and coordinators: who runs the diary
MONEY = 'money'  # owner and admins: who runs the money
OWNERS = 'owners'  # who pays Kyo


@dataclass(frozen=True)
class Rule:
    # (verb, audience) per push row.
    push: tuple = ()
    # WhatsApp from Kyo's number, to the opted-in clients.
    client_whatsapp: bool = False


RULES = {
    BOOKED: Rule(push=((Verb.APPOINTMENT_BOOKED, PROFESSIONAL),)),
    MOVED: Rule(push=((Verb.APPOINTMENT_RESCHEDULED, PROFESSIONAL),)),
    CHANGED: Rule(push=((Verb.APPOINTMENT_UPDATED, PROFESSIONAL),)),
    # The new professional has a turno booked into their day; the old one lost it.
    REASSIGNED: Rule(push=(
        (Verb.APPOINTMENT_BOOKED, PROFESSIONAL),
        (Verb.APPOINTMENT_REASSIGNED, PREVIOUS),
    )),
    CANCELLED: Rule(push=((Verb.APPOINTMENT_CANCELLED, PROFESSIONAL),)),
    REQUESTED: Rule(push=((Verb.APPOINTMENT_REQUESTED, PROFESSIONAL),), client_whatsapp=True),
    # The client hears the answer from the business's own WhatsApp: the turno
    # sheet's "Avisar al cliente" step, owed through Appointment.answer_notice.
    ACCEPTED: Rule(push=((Verb.APPOINTMENT_CONFIRMED, PROFESSIONAL),)),
    REJECTED: Rule(push=((Verb.APPOINTMENT_REJECTED, PROFESSIONAL),)),
    # The professional never acted; telling them would be noise.
    EXPIRED: Rule(client_whatsapp=True),
    # Their own notice went unseen; the ones who run the diary are next.
    WAITING: Rule(push=((Verb.APPOINTMENT_REQUEST_WAITING, MANAGERS),)),
    DIGEST: Rule(push=((Verb.PLAN_DIGEST, MONEY),)),
    BILLING_DUE: Rule(push=((Verb.BILLING_DUE, OWNERS),)),
}

# Undated notices that open a screen showing the current state: yesterday's is
# the same row as today's, only stale. Each one replaces the last, so the bell
# never fills up with twenty identical "Resumen de cobros".
REPLACES = (Verb.PLAN_DIGEST, Verb.BILLING_DUE)

# ponytail: no sender yet. Until Meta's Cloud API sends the outbox nothing is
# filed and the public page does not offer the box. The Cloud API turns this on.
WHATSAPP_SENDER = False


def client_whatsapp_on(tenant):
    """
    Whether the clients of a public request are written to at all. Needs a
    sender AND the business's own number: every message gives it as the
    contact, and a message nobody can answer is worse than none.
    """
    return WHATSAPP_SENDER and bool(tenant.phone)


ROLES = {
    MANAGERS: (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.COORDINATOR),
    MONEY: (Membership.Role.OWNER, Membership.Role.ADMIN),
    OWNERS: (Membership.Role.OWNER,),
}


def notify(event, *, appointment=None, tenant=None, actor=None, previous=None):
    """
    Tell whoever `event` concerns. `appointment` for a turno's events, `tenant`
    for the business's own; `actor` is the member who did it (None for the
    public page and the sweep); `previous` the professional a turno was taken
    from.
    """
    rule = RULES[event]
    tenant = tenant or appointment.tenant

    rows = []
    for verb, audience in rule.push:
        recipients = [
            recipient for recipient in _audience(audience, tenant, appointment, previous)
            if actor is None or recipient.pk != actor.pk
        ]
        if verb in REPLACES:
            Notification.objects.filter(recipient__in=recipients, verb=verb).delete()
        rows += [Notification(recipient=r, actor=actor, appointment=appointment, verb=verb) for r in recipients]
    Notification.objects.bulk_create(rows)

    if rule.client_whatsapp and client_whatsapp_on(tenant):
        _file_whatsapp(event, appointment)


def edited(appointment, *, actor, was_with, was_at):
    """A teammate saved an edit of a turno: which of the four it was."""
    if appointment.professional_id != was_with.pk:
        notify(REASSIGNED, appointment=appointment, actor=actor, previous=was_with)
    elif appointment.start != was_at:
        notify(MOVED, appointment=appointment, actor=actor)
    else:
        notify(CHANGED, appointment=appointment, actor=actor)


def _audience(audience, tenant, appointment, previous):
    if audience == PROFESSIONAL:
        return [appointment.professional]
    if audience == PREVIOUS:
        return [previous]
    members = tenant.memberships.filter(role__in=ROLES[audience], status=Membership.Status.ACTIVE)
    if audience == MANAGERS:
        # Already told, as the request's professional.
        members = members.exclude(pk=appointment.professional_id)
    return list(members)


def _file_whatsapp(event, appointment):
    shop = appointment.tenant
    facts = {
        'shop': shop.name,
        'shop_phone': shop.phone.as_international,
        'service': appointment.service.name,
        'when': _when(appointment),
        'pro': appointment.professional.display_name(),
    }
    OutboundMessage.objects.bulk_create([
        OutboundMessage(
            tenant=shop, appointment=appointment, to=client.phone,
            body=_to_client(event, name=client.name.split()[0], **facts),
        )
        for client in appointment.clients.all()
        if client.whatsapp_opt_in and client.phone
    ])


# Not the locale's names: the server's locale is not something to depend on for
# the one word that tells somebody which day to show up.
WEEKDAYS = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')


def _when(appointment):
    local = appointment.start.astimezone(ZoneInfo(appointment.tenant.timezone))
    return f'{WEEKDAYS[local.weekday()]} {local:%d/%m} a las {local:%H:%M}'


def _to_client(event, *, name, shop, shop_phone, service, when, pro):
    if event == REQUESTED:
        return (f'Hola {name}, recibimos tu pedido de turno en {shop}: {service}, {when}, '
                f'con {pro}. Te avisamos por acá cuando lo confirmen. Consultas al {shop_phone}.')
    return (f'Hola {name}, en {shop} no llegaron a responder a tiempo tu pedido de {service} '
            f'del {when}. Podés pedir otro horario o escribir al {shop_phone}.')
