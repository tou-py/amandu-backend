import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

User = get_user_model()


@pytest.mark.django_db
def test_user_belongs_to_many_tenants_with_different_roles():
    user = User.objects.create_user(email='staff@example.com', password='pw')
    salon = Tenant.objects.create(name='Salon', slug='salon')
    clinic = Tenant.objects.create(name='Clinic', slug='clinic')

    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)
    Membership.objects.create(user=user, tenant=clinic, role=Membership.Role.STAFF)

    # The M2M sugar reaches both sides.
    assert set(user.tenants.all()) == {salon, clinic}
    assert set(salon.users.all()) == {user}
    assert user.memberships.get(tenant=salon).role == Membership.Role.OWNER


@pytest.mark.django_db
def test_membership_pairing_is_unique():
    user = User.objects.create_user(email='dup@example.com', password='pw')
    salon = Tenant.objects.create(name='Salon', slug='salon')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.STAFF)

    with pytest.raises(IntegrityError):
        # Same pairing again: the role is what varies, never the pair.
        Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)


@pytest.mark.django_db
def test_superuser_belongs_to_no_tenant():
    root = User.objects.create_superuser(email='root@example.com', password='pw')

    # Platform-level account: zero memberships, not a null FK.
    assert root.tenants.count() == 0
