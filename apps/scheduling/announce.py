"""
Who hears about a public request, and how: the one place that decides it.

A request off the public page goes through three moments -- asked for,
accepted, turned down -- and each one is news to two people: the client who
asked, and the professional whose diary it lands in. The client has no account
and no device we can push to, so they hear by WhatsApp, and only if they ticked
the box. The professional hears by push (a Notification row the sweep delivers)
and also by WhatsApp when they saved a phone.

Never about something the professional did themselves: accepting your own
request is not news to you. Same rule as AppointmentViewSet.cancel.

Called inside the transaction that made the change, so a turn and its notices
exist together or not at all. Nothing is sent from here; the sweep sends.
"""

import random
from zoneinfo import ZoneInfo

from apps.accounts.models import Notification
from apps.scheduling import whatsapp
from apps.scheduling.models import OutboundMessage

REQUESTED = 'requested'
ACCEPTED = 'accepted'
REJECTED = 'rejected'

VERBS = {
    REQUESTED: Notification.Verb.APPOINTMENT_REQUESTED,
    ACCEPTED: Notification.Verb.APPOINTMENT_CONFIRMED,
    REJECTED: Notification.Verb.APPOINTMENT_REJECTED,
}

# Not the locale's names: the server's locale is not something to depend on for
# the one word that tells somebody which day to show up.
WEEKDAYS = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')


def announce(appointment, event, actor=None, reason=''):
    professional = appointment.professional
    own_doing = actor is not None and actor.pk == professional.pk
    if not own_doing:
        Notification.objects.create(
            recipient=professional, actor=actor, appointment=appointment, verb=VERBS[event],
        )

    shop = appointment.tenant
    if not whatsapp.enabled_for(shop):
        return

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
    rows = [
        OutboundMessage(
            tenant=shop, appointment=appointment, to=client.phone,
            body=_to_client(event, name=client.name.split()[0], **facts),
        )
        for client in clients
        if client.whatsapp_opt_in and client.phone
    ]
    if not own_doing and professional.user.phone:
        rows.append(OutboundMessage(
            tenant=shop, appointment=appointment, to=professional.user.phone,
            body=_to_professional(event, **facts),
        ))
    OutboundMessage.objects.bulk_create(rows)


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
