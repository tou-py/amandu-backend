import pytest
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant
from apps.tenancy.permissions import HasActiveMembership


def make_request(user, tenant_id=None):
    """A DRF request with the caller authenticated, optionally selecting a tenant."""
    extra = {} if tenant_id is None else {'HTTP_X_TENANT_ID': str(tenant_id)}
    request = Request(APIRequestFactory().get('/', **extra))
    request.user = user
    return request


def allows(request):
    return HasActiveMembership().has_permission(request, view=None)


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user(email='u@example.com', password='pw')


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic')


def test_single_membership_needs_no_header(user, salon):
    membership = Membership.objects.create(user=user, tenant=salon)
    request = make_request(user)

    assert allows(request)
    assert request.tenant == salon
    assert request.membership == membership


def test_header_selects_among_several_memberships(user, salon, clinic):
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)
    Membership.objects.create(user=user, tenant=clinic, role=Membership.Role.STAFF)
    request = make_request(user, clinic.pk)

    assert allows(request)
    assert request.tenant == clinic
    assert request.membership.role == Membership.Role.STAFF


def test_several_memberships_without_header_is_refused(user, salon, clinic):
    Membership.objects.create(user=user, tenant=salon)
    Membership.objects.create(user=user, tenant=clinic)

    assert not allows(make_request(user))


def test_header_for_a_tenant_the_user_does_not_belong_to_is_refused(
    user, salon, clinic
):
    Membership.objects.create(user=user, tenant=salon)

    assert not allows(make_request(user, clinic.pk))


def test_suspended_membership_is_refused(user, salon):
    Membership.objects.create(
        user=user, tenant=salon, status=Membership.Status.SUSPENDED
    )

    assert not allows(make_request(user))
    assert not allows(make_request(user, salon.pk))


def test_suspended_tenant_is_refused(user, salon):
    Membership.objects.create(user=user, tenant=salon)
    salon.status = Tenant.Status.SUSPENDED
    salon.save()

    assert not allows(make_request(user, salon.pk))


def test_unparseable_header_is_refused_not_an_error(user, salon):
    Membership.objects.create(user=user, tenant=salon)

    # Would raise ValueError if the header were fed straight into a lookup.
    assert not allows(make_request(user, 'not-an-id'))


def test_superuser_without_memberships_is_refused(db, django_user_model, salon):
    root = django_user_model.objects.create_superuser(
        email='root@example.com', password='pw'
    )

    assert not allows(make_request(root))
    assert not allows(make_request(root, salon.pk))
