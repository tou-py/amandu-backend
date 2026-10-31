"""
WhatsApp, through a self-hosted WAHA (https://waha.devlike.pro) paired with ONE
Kyo number that speaks for every business.

WAHA drives WhatsApp Web; it is not Meta's official API, and a number that
behaves like a bot gets restricted or banned -- for every business at once,
since they share it. Everything below that looks slow is on purpose, taken from
WAHA's own "How to avoid blocking" guide:

  * Sent only from the sweep, never on the spot, and at most PER_TICK a run:
    a burst of messages to people who never wrote first is the pattern that
    gets numbers banned.
  * A random GAP between recipients, and "typing" before each message.
  * Never at night in the business's own timezone (WAKING_HOURS).
  * At most PER_HOUR messages to one number in an hour, the guide's figure.
  * Before the first send of a run, WAHA is asked whether WhatsApp is holding
    back messages to new contacts (463 timelock, 475 capping at any warning
    stage); while it is, nothing goes out. Almost every message here is to a
    new contact, so a run in that state only digs the hole deeper.
  * The first failure ends the run instead of hammering a session that may be
    shadow-restricted or unpaired. MAX_ATTEMPTS caps retries.
    Nothing here ever restarts, logs out or re-pairs the session: the guide is
    explicit that both restrictions lift on their own and a restart makes it
    worse.
  * Short, personal messages (first name, the turno's own details, a greeting
    that varies -- apps/scheduling/notify.py), no links in them.
  * The strongest one is not here: the public page offers a wa.me link so the
    client writes to the Kyo number first, and what we send becomes a reply
    (settings.WHATSAPP_NUMBER).

Guide: https://waha.devlike.pro/docs/overview/how-to-avoid-blocking/

`send` is the whole contract with WAHA, and the one thing tests replace.
Moving to Meta's Cloud API later means rewriting that function (with approved
templates), nothing else.

ponytail: PER_TICK x 12 ticks an hour = 36 messages/hour ceiling across ALL
tenants. Raise it with care, or move to the Cloud API, when volume needs it.
ponytail: no inbound side -- a client answering "BAJA" is not read. Add a WAHA
webhook that clears Client.whatsapp_opt_in when replies start to matter.
"""

import logging
import random
import time
from datetime import timedelta
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.utils import timezone

from apps.scheduling.models import OutboundMessage

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
PER_TICK = 3
PER_HOUR = 4  # messages to one number in any hour
GAP = (30, 60)  # seconds between two recipients
TYPING = (2, 8)  # seconds of "typing..." before a message, by its length
WAKING_HOURS = range(8, 21)  # local hours a message may go out in
TIMEOUT = 10  # seconds per HTTP call; unset, one hung call froze the sweep before


def enabled_for(tenant):
    """
    Whether messages are filed for this business at all. Needs WAHA configured
    AND the business's own number: every message gives it as the contact, and
    a message nobody can answer is worse than none.
    """
    return bool(settings.WAHA_URL and tenant.phone)


def send(to, text):
    """
    Show "typing" for about as long as a person would take to write `text`,
    then send it. Raises on any failure.

    No sendSeen first, unlike WAHA's reply flow: these chats are ones we open,
    there is nothing in them to have seen.
    """
    chat = {'chatId': f'{str(to).lstrip("+")}@c.us'}
    _post('startTyping', chat)
    _pause(typing_time(text))
    _post('stopTyping', chat)
    _post('sendText', {**chat, 'text': text})


def typing_time(text):
    """Seconds of "typing": grows with the message, capped, never exact."""
    low, high = TYPING
    return min(high, low + len(text) / 60) + random.uniform(0, 1.5)


def _post(endpoint, payload):
    response = requests.post(
        f'{settings.WAHA_URL.rstrip("/")}/api/{endpoint}',
        json={'session': settings.WAHA_SESSION, **payload},
        headers={'X-Api-Key': settings.WAHA_API_KEY},
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def restricted():
    """
    Whether WhatsApp is holding back messages to new contacts: a 463 timelock,
    or a 475 cap at any stage (FIRST_WARNING already means slow down). Raises
    if WAHA cannot say.
    """
    timelock = _get('timelock')
    capping = _get('capping')
    return bool(timelock.get('isActive')) or capping.get('cappingStatus', 'NONE') != 'NONE'


def _get(what):
    response = requests.get(
        f'{settings.WAHA_URL.rstrip("/")}/api/sessions/{settings.WAHA_SESSION}/{what}',
        headers={'X-Api-Key': settings.WAHA_API_KEY},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _pause(seconds):
    time.sleep(seconds)


def deliver_due():
    """
    Send what is waiting, oldest first, paced. Returns (sent, failed).

    Runs under the sweep's lock, which is what keeps two runs from sending the
    same row: nothing else ever sends.
    """
    waiting = (
        OutboundMessage.objects
        .filter(sent_at__isnull=True, attempts__lt=MAX_ATTEMPTS)
        .select_related('tenant')
    )
    sent = 0
    for message in waiting.iterator():
        if sent >= PER_TICK:
            break
        if timezone.localtime(timezone=ZoneInfo(message.tenant.timezone)).hour not in WAKING_HOURS:
            continue
        if OutboundMessage.objects.filter(
            to=message.to, sent_at__gte=timezone.now() - timedelta(hours=1),
        ).count() >= PER_HOUR:
            continue
        if sent:
            _pause(random.uniform(*GAP))
        else:
            # Asked only once there is something to send: an empty outbox
            # costs WAHA nothing.
            try:
                if restricted():
                    logger.warning('WhatsApp is limiting new contacts; the outbox waits.')
                    return 0, False
            except Exception:
                logger.exception('WAHA could not say whether the number is restricted.')
                return 0, True
        message.attempts += 1
        try:
            send(message.to, message.body)
        except Exception as exc:
            message.last_error = repr(exc)[:1000]
            message.save(update_fields=['attempts', 'last_error'])
            return sent, True
        message.sent_at = timezone.now()
        message.save(update_fields=['attempts', 'sent_at'])
        sent += 1
    return sent, False
