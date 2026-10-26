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
  * WhatsApp from Kyo's number (WAHA, whatsapp.py): to the client of a public
    request who ticked the box, and to a professional who saved a phone. Filed
    here as an OutboundMessage; only the sweep sends, paced.

One rule for everyone: nobody is told about what they did themselves.

Called inside the transaction that made the change, so a turno and its notices
exist together or not at all.

The professional's reminder before a turno is not here: it is not news about
a change, it is computed from the clock and pushed by the sweep directly
(send_reminders._run), stamped on the appointment so it goes once.
"""

import random
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from apps.accounts.models import Membership, Notification
from apps.scheduling import whatsapp
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
    # WhatsApp from Kyo's number: to the opted-in clients, to the professional.
    client_whatsapp: bool = False
    professional_whatsapp: bool = False


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
    REQUESTED: Rule(
        push=((Verb.APPOINTMENT_REQUESTED, PROFESSIONAL),),
        client_whatsapp=True, professional_whatsapp=True,
    ),
    ACCEPTED: Rule(
        push=((Verb.APPOINTMENT_CONFIRMED, PROFESSIONAL),),
        client_whatsapp=True, professional_whatsapp=True,
    ),
    REJECTED: Rule(
        push=((Verb.APPOINTMENT_REJECTED, PROFESSIONAL),),
        client_whatsapp=True, professional_whatsapp=True,
    ),
    # The professional never acted; telling them would be noise.
    EXPIRED: Rule(client_whatsapp=True),
    # Their own notice went unseen; the ones who run the diary are next.
    WAITING: Rule(push=((Verb.APPOINTMENT_REQUEST_WAITING, MANAGERS),)),
    DIGEST: Rule(push=((Verb.PLAN_DIGEST, MONEY),)),
    BILLING_DUE: Rule(push=((Verb.BILLING_DUE, OWNERS),)),
}

ROLES = {
    MANAGERS: (Membership.Role.OWNER, Membership.Role.ADMIN, Membership.Role.COORDINATOR),
    MONEY: (Membership.Role.OWNER, Membership.Role.ADMIN),
    OWNERS: (Membership.Role.OWNER,),
}


def notify(event, *, appointment=None, tenant=None, actor=None, previous=None, reason=''):
    """
    Tell whoever `event` concerns. `appointment` for a turno's events, `tenant`
    for the business's own; `actor` is the member who did it (None for the
    public page and the sweep); `previous` the professional a turno was taken
    from; `reason` what the shop typed when turning a request down.
    """
    rule = RULES[event]
    tenant = tenant or appointment.tenant

    rows = []
    for verb, audience in rule.push:
        for recipient in _audience(audience, tenant, appointment, previous):
            if actor is not None and recipient.pk == actor.pk:
                continue
            rows.append(Notification(recipient=recipient, actor=actor, appointment=appointment, verb=verb))
    Notification.objects.bulk_create(rows)

    if (rule.client_whatsapp or rule.professional_whatsapp) and whatsapp.enabled_for(tenant):
        _file_whatsapp(event, rule, appointment, actor, reason)


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


def _file_whatsapp(event, rule, appointment, actor, reason):
    shop = appointment.tenant
    professional = appointment.professional
    clients = list(appointment.clients.all())
    facts = {
        'shop': shop.name,
        'shop_phone': shop.phone.as_international,
        'service': appointment.service.name,
        'when': _when(appointment),
        'pro': professional.display_name(),
        'clients': ', '.join(client.name for client in clients),
        'actor': actor.display_name() if actor else '',
        'reason': reason,
    }
    rows = []
    if rule.client_whatsapp:
        rows += [
            OutboundMessage(
                tenant=shop, appointment=appointment, to=client.phone,
                body=_to_client(event, name=client.name.split()[0], **facts),
            )
            for client in clients
            if client.whatsapp_opt_in and client.phone
        ]
    own_doing = actor is not None and actor.pk == professional.pk
    if rule.professional_whatsapp and not own_doing and professional.user.phone:
        rows.append(OutboundMessage(
            tenant=shop, appointment=appointment, to=professional.user.phone,
            body=_to_professional(event, **facts),
        ))
    OutboundMessage.objects.bulk_create(rows)


# Not the locale's names: the server's locale is not something to depend on for
# the one word that tells somebody which day to show up.
WEEKDAYS = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')


def _when(appointment):
    local = appointment.start.astimezone(ZoneInfo(appointment.tenant.timezone))
    return f'{WEEKDAYS[local.weekday()]} {local:%d/%m} a las {local:%H:%M}'


# Said a few ways on purpose: the same sentence to every client is one of the
# patterns WhatsApp reads as a bot (WAHA's "How to avoid blocking").
GREETINGS = ('Hola', 'Buenas', 'Qué tal')


def _to_client(event, *, name, shop, shop_phone, service, when, pro, reason, **_):
    hello = random.choice(GREETINGS)
    if event == REQUESTED:
        return (f'{hello} {name}, recibimos tu pedido de turno en {shop}: {service}, {when}, '
                f'con {pro}. Te avisamos por acá cuando lo confirmen. Consultas al {shop_phone}.')
    if event == ACCEPTED:
        return (f'{hello} {name}, {shop} confirmó tu turno: {service}, {when}, con {pro}. '
                f'Consultas al {shop_phone}.')
    if event == EXPIRED:
        return (f'{hello} {name}, en {shop} no llegaron a responder a tiempo tu pedido de {service} '
                f'del {when}. Podés pedir otro horario o escribir al {shop_phone}.')
    because = f' Motivo: {reason}.' if reason else ''
    return (f'{hello} {name}, {shop} no puede tomar tu turno de {service} del {when}.{because} '
            f'Podés pedir otro horario o escribir al {shop_phone}.')


def _to_professional(event, *, shop, service, when, clients, actor, **_):
    if event == REQUESTED:
        return (f'Nuevo pedido de turno en {shop}: {service}, {when}, de {clients}. '
                f'Confirmalo o rechazalo en Kyo.')
    if event == ACCEPTED:
        return f'{actor} confirmó tu turno en {shop}: {service}, {when}, con {clients}.'
    return f'{actor} rechazó el pedido de {clients} en {shop}: {service}, {when}.'
