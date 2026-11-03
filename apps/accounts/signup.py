"""
Someone creating their own business. A user, a tenant and an owner membership
are one fact, so they are created together or not at all.

The verification token is signed rather than stored: there is nothing to clean
up, and it dies by age. Replays are stopped by the user already being verified.
"""
from datetime import timedelta

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.accounts.models import CustomUser, Membership
from apps.tenancy.models import Tenant

VERIFY_SALT = 'verify-email'
VERIFY_MAX_AGE = 3 * 24 * 60 * 60  # seconds


# How long a signup nobody confirmed stays around, holding its address and slug.
UNVERIFIED_DAYS = 7


def verify_token(user):
    return signing.dumps(user.pk, salt=VERIFY_SALT)


def user_from_token(token):
    """The user a verification link names, or None if it is bad, old or theirs is gone."""
    try:
        pk = signing.loads(token, salt=VERIFY_SALT, max_age=VERIFY_MAX_AGE)
    except signing.BadSignature:  # SignatureExpired is one
        return None
    return CustomUser.objects.filter(pk=pk).first()


def _unique_slug(name):
    room = Tenant._meta.get_field('slug').max_length - 4  # for a "-999"
    base = slugify(name)[:room].strip('-') or 'negocio'
    slug, n = base, 1
    while Tenant.objects.filter(slug=slug).exists():
        n += 1
        slug = f'{base}-{n}'
    return slug


def _browser_guess(field_name, value):
    """Nobody chose the timezone or the country, the browser did: a value the
    tenant would refuse falls back to the default instead of failing the signup."""
    field = Tenant._meta.get_field(field_name)
    try:
        return field.clean(value, None)
    except ValidationError:
        return field.get_default()


@transaction.atomic
def sweep_unverified():
    """Drop signups never confirmed, with the business they opened: nobody could
    log in, so nobody worked in it. Accepting an invitation confirms the address,
    so an unconfirmed user has no other business to lose."""
    stale = CustomUser.objects.filter(
        email_verified_at__isnull=True,
        created_at__lt=timezone.now() - timedelta(days=UNVERIFIED_DAYS),
    )
    Tenant.objects.filter(memberships__user__in=stale, memberships__role=Membership.Role.OWNER).delete()
    return stale.delete()[0]


@transaction.atomic
def create_business(*, business_name, first_name, email, password, timezone='', country=''):
    user = CustomUser.objects.create_user(
        email=email, password=password, first_name=first_name, email_verified_at=None,
    )
    user.accept_terms()
    tenant = Tenant.objects.create(
        name=business_name,
        slug=_unique_slug(business_name),
        timezone=_browser_guess('timezone', timezone),
        country=_browser_guess('country', country),
    )
    Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER, attends_appointments=True)
    return user
