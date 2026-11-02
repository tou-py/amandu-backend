"""
Someone creating their own business. A user, a tenant and an owner membership
are one fact, so they are created together or not at all.

The verification token is signed rather than stored: there is nothing to clean
up, and it dies by age. Replays are stopped by the user already being verified.
"""
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.text import slugify

from apps.accounts.models import CustomUser, Membership
from apps.tenancy.models import Tenant

VERIFY_SALT = 'verify-email'
VERIFY_MAX_AGE = 3 * 24 * 60 * 60  # seconds


def verify_token(user):
    return signing.dumps(user.pk, salt=VERIFY_SALT)


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
