"""
Managing the team: changing someone's role, whether they attend turnos, and
suspending or reactivating them. Through the HTTP API the team screen uses.

Who may touch whom: owner and admins manage; an admin manages coordinators and
staff, only the owner manages admins; nobody changes themselves or the owner,
and nobody is made owner here.
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Client, Service
from apps.tenancy import pricing
from apps.tenancy.models import Tenant

LIST_URL = reverse('accounts:member-list')


def detail_url(member):
    return reverse('accounts:member-detail', args=[member.pk])


def api(member):
    http = APIClient()
    http.force_authenticate(user=member.user)
    http.credentials(HTTP_X_TENANT_ID=str(member.tenant_id))
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salon', slug='salon', country='PY',
        paid_until=timezone.localdate() + timedelta(days=20),
    )


@pytest.fixture
def make(django_user_model, salon):
    def _make(email, role, attends=False):
        user = django_user_model.objects.create_user(email=email, password='pw')
        return Membership.objects.create(user=user, tenant=salon, role=role, attends_appointments=attends)

    return _make


@pytest.fixture
def owner(make):
    return make('owner@example.com', Membership.Role.OWNER, attends=True)


@pytest.fixture
def admin(make):
    return make('admin@example.com', Membership.Role.ADMIN)


@pytest.fixture
def staff(make):
    return make('staff@example.com', Membership.Role.STAFF, attends=True)


def patch(actor, member, **body):
    return api(actor).patch(detail_url(member), body, format='json')


def test_an_admin_changes_a_staff_members_role(admin, staff):
    res = patch(admin, staff, role='coordinator')

    assert res.status_code == 200, res.data
    staff.refresh_from_db()
    assert staff.role == Membership.Role.COORDINATOR


def test_an_admin_cannot_touch_another_admin_or_make_one(make, admin, staff):
    other = make('other@example.com', Membership.Role.ADMIN)

    assert patch(admin, other, role='staff').status_code == 403
    assert patch(admin, staff, role='admin').status_code == 403


def test_the_owner_manages_admins(owner, admin):
    assert patch(owner, admin, role='coordinator').status_code == 200


def test_nobody_changes_themselves_or_the_owner(owner, admin):
    assert patch(admin, admin, role='staff').status_code == 403
    assert patch(admin, owner, attends_appointments=False).status_code == 403
    assert patch(owner, owner, status='suspended').status_code == 403


def test_nobody_is_made_owner_here(owner, staff):
    assert patch(owner, staff, role='owner').status_code == 400


def test_staff_and_coordinators_manage_nobody(make, staff):
    coordinator = make('c@example.com', Membership.Role.COORDINATOR)

    assert patch(coordinator, staff, role='coordinator').status_code == 403


def test_suspending_cuts_access_and_reactivating_restores_it(admin, make):
    receptionist = make('r@example.com', Membership.Role.STAFF)

    assert patch(admin, receptionist, status='suspended').status_code == 200
    assert api(receptionist).get(LIST_URL).status_code == 403

    assert patch(admin, receptionist, status='active').status_code == 200
    receptionist.refresh_from_db()
    assert receptionist.status == Membership.Status.ACTIVE


def test_leaving_the_agenda_with_turnos_ahead_is_refused(salon, admin, staff):
    service = Service.objects.create(tenant=salon, name='Corte', duration=timedelta(minutes=30))
    start = timezone.now() + timedelta(days=2)
    turno = Appointment.objects.create(
        tenant=salon, professional=staff, service=service, start=start, end=start + service.duration,
    )
    turno.clients.add(Client.objects.create(tenant=salon, name='Ana'))

    for body in ({'attends_appointments': False}, {'status': 'suspended'}):
        res = patch(admin, staff, **body)
        assert res.status_code == 409, body
        assert res.data['code'] == 'member_has_turnos'

    staff.refresh_from_db()
    assert staff.attends_appointments and staff.status == Membership.Status.ACTIVE


def test_each_member_says_what_their_agenda_change_costs(owner, admin, staff):
    """Two professionals attend (owner, staff): Equipo. Taking one out drops
    the extra; putting the admin in adds one."""
    res = api(admin).get(LIST_URL)

    assert res.status_code == 200
    by_email = {row['email']: row for row in res.data}
    now = pricing.BASE + pricing.PER_EXTRA_PROFESSIONAL
    assert by_email['staff@example.com']['agenda_change'] == {
        'monthly_from': now, 'monthly_to': pricing.BASE,
    }
    assert by_email['admin@example.com']['agenda_change'] == {
        'monthly_from': now, 'monthly_to': now + pricing.PER_EXTRA_PROFESSIONAL,
    }


def test_a_business_on_agreement_sees_no_price(salon, admin, staff):
    salon.paid_until = None
    salon.save()

    rows = api(admin).get(LIST_URL).data

    assert all(row['agenda_change'] is None for row in rows)


def test_invitations_follow_the_same_rules(owner, admin):
    url = reverse('accounts:invitation-list')

    def invite(actor, role):
        return api(actor).post(url, {'email': f'{role}@new.com', 'role': role}, format='json')

    assert invite(admin, 'admin').status_code == 403
    assert invite(owner, 'owner').status_code == 400
    assert invite(owner, 'admin').status_code == 201
