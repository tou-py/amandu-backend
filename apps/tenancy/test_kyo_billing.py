"""
What a tenant pays Kyo and where it stands on paying it (apps/tenancy/pricing.py),
the Cobros add-on gate, and the owner's subscription endpoint.

The sweep side (suspending, the daily notice) lives with the sweep in
apps/scheduling/test_send_reminders.py; the read-only refusal with the
permission in test_permissions.py.
"""
from datetime import date, timedelta

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy import pricing
from apps.tenancy.models import Tenant

TODAY = date(2026, 10, 14)
SUBSCRIPTION_URL = reverse('tenancy:tenant-subscription')


def tenant_paid_until(days, status=Tenant.Status.ACTIVE):
    return Tenant(
        name='Studio', slug='studio', status=status,
        paid_until=None if days is None else TODAY + timedelta(days=days),
    )


@pytest.mark.parametrize(
    'days, expected',
    [
        (None, pricing.OK),  # pilots and courtesy accounts never come due
        (4, pricing.OK),
        (3, pricing.DUE),  # the owner starts being told
        (0, pricing.DUE),  # paid through today means working all of today
        (-1, pricing.READ_ONLY),
        (-3, pricing.READ_ONLY),
        # Past the window but not yet swept: still read-only, never "ok".
        (-4, pricing.READ_ONLY),
    ],
)
def test_billing_state_follows_paid_until(days, expected):
    assert pricing.billing_state(tenant_paid_until(days), TODAY) == expected


@pytest.mark.parametrize('status, expected', [
    (Tenant.Status.SUSPENDED, pricing.SUSPENDED),
    (Tenant.Status.CLOSED, pricing.CLOSED),
])
def test_the_status_wins_over_the_dates(status, expected):
    """A tenant suspended by hand is suspended whatever it paid for."""
    assert pricing.billing_state(tenant_paid_until(30, status), TODAY) == expected


def total(professionals, cobros=False, facturacion=False):
    tenant = Tenant(addon_cobros=cobros, addon_facturacion=facturacion)
    return sum(line['amount'] for line in pricing.monthly_lines(tenant, professionals))


@pytest.mark.parametrize('professionals, expected', [
    # Nobody attends yet: the base is the agenda, it is still paid.
    (0, 100_000),
    (1, 100_000),
    (2, 150_000),
    (3, 200_000),
])
def test_the_month_is_the_base_plus_each_extra_professional(professionals, expected):
    assert total(professionals) == expected


def test_add_ons_are_added_on_top():
    assert total(3, cobros=True) == 280_000
    assert total(1, cobros=True, facturacion=True) == 305_000


@pytest.mark.parametrize('professionals, plan', [(0, 'solo'), (1, 'solo'), (2, 'equipo')])
def test_the_plan_is_named_after_the_team(professionals, plan):
    assert pricing.plan_name(professionals) == plan


# --- Over HTTP ---------------------------------------------------------------


def api(user, tenant):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def studio(db):
    return Tenant.objects.create(name='Studio', slug='studio')


@pytest.fixture
def member(db, django_user_model):
    def make(tenant, role=Membership.Role.OWNER, attends=False, name='owner'):
        user = django_user_model.objects.create_user(
            email=f'{name}@example.com', password='pw', first_name=name.title()
        )
        return Membership.objects.create(user=user, tenant=tenant, role=role, attends_appointments=attends)
    return make


def test_cobros_is_refused_without_the_add_on(studio, member):
    owner = member(studio)

    res = api(owner.user, studio).get(reverse('scheduling:plan-list'))

    assert res.status_code == 403
    assert res.data['code'] == 'addon_required'


def test_cobros_answers_with_the_add_on(studio, member):
    owner = member(studio)
    studio.addon_cobros = True
    studio.save()

    assert api(owner.user, studio).get(reverse('scheduling:plan-list')).status_code == 200


def test_the_client_file_itself_is_not_cobros(studio, member):
    """Only taking money and reading payments are gated; the file is the agenda's."""
    owner = member(studio)

    assert api(owner.user, studio).get(reverse('scheduling:client-list')).status_code == 200


def test_the_owner_reads_what_the_month_costs(studio, member, settings):
    settings.KYO_CONTACT_WHATSAPP = '595981000000'
    owner = member(studio, attends=True, name='ana')
    member(studio, role=Membership.Role.STAFF, attends=True, name='bea')
    member(studio, role=Membership.Role.COORDINATOR, name='desk')  # does not attend: free
    studio.addon_cobros = True
    studio.paid_until = date.today() + timedelta(days=10)
    studio.save()

    res = api(owner.user, studio).get(SUBSCRIPTION_URL)

    assert res.status_code == 200
    assert res.data['plan'] == 'equipo'
    assert res.data['professionals'] == ['Ana', 'Bea']
    assert res.data['addons'] == ['cobros']
    assert [line['concept'] for line in res.data['lines']] == ['base', 'extra_professionals', 'cobros']
    assert res.data['total'] == 230_000
    assert res.data['billing'] == 'ok'
    assert res.data['contact_whatsapp'] == '595981000000'
    assert res.data['prices']['per_extra_professional'] == 50_000


@pytest.mark.parametrize('role', [Membership.Role.ADMIN, Membership.Role.STAFF])
def test_only_the_owner_reads_the_subscription(studio, member, role):
    someone = member(studio, role=role, name='someone')

    assert api(someone.user, studio).get(SUBSCRIPTION_URL).status_code == 403


def test_the_subscription_stays_readable_in_the_read_only_window(studio, member):
    """It is where the owner finds how to pay."""
    owner = member(studio)
    studio.paid_until = date.today() - timedelta(days=1)
    studio.save()

    res = api(owner.user, studio).get(SUBSCRIPTION_URL)

    assert res.status_code == 200
    assert res.data['billing'] == 'read_only'
    assert res.data['suspends_after'] == str(studio.paid_until + timedelta(days=3))
