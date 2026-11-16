import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

User = get_user_model()


@pytest.mark.django_db
def test_membership_pairing_is_unique():
    user = User.objects.create_user(email='dup@example.com', password='pw')
    salon = Tenant.objects.create(name='Salon', slug='salon')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.STAFF)

    with pytest.raises(IntegrityError):
        # Same pairing again: the role is what varies, never the pair.
        Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)
