from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Service
from apps.tenancy.models import Tenant

LIST_URL = reverse('accounting:cashentry-list')


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic', country='PY')


@pytest.fixture
def manager(db, django_user_model, salon):
    """Somebody with standing over the money: the book is admin-and-up."""
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.ADMIN)
    return user


@pytest.fixture
def stylist(db, django_user_model, salon):
    """The same shop, no standing over its figures."""
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.STAFF)
    return user


@pytest.fixture
def receptionist(db, django_user_model, salon):
    """The front desk: takes the money all day, does not read the book."""
    user = django_user_model.objects.create_user(email='c@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


def make_appointment(tenant, user=None, email=None, start=None):
    """A bookable slot in `tenant`, with the staff and service it needs."""
    if user is None:
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(email=email, password='pw')
    membership = Membership.objects.filter(user=user, tenant=tenant).first()
    if membership is None:
        membership = Membership.objects.create(user=user, tenant=tenant)
    service = Service.objects.create(
        tenant=tenant, name=f'Haircut {tenant.slug}', duration=timedelta(minutes=30)
    )
    start = start or datetime(2026, 3, 2, 10, 0, tzinfo=ZoneInfo('UTC'))
    return Appointment.objects.create(
        tenant=tenant,
        professional=membership,
        service=service,
        start=start,
        end=start + service.duration,
    )


# --- writing the book --------------------------------------------------------

def test_income_takes_its_tenant_from_the_request_not_the_payload(manager, salon, clinic):
    """`tenant` is editable=False, so a payload naming another shop is ignored,
    not obeyed -- this is the whole reason it is not a serializer field."""
    res = api(manager, salon).post(
        LIST_URL,
        {
            'kind': 'income',
            'amount': 150000,
            'occurred_on': '2026-03-02',
            'concept': 'Haircut',
            'tenant': clinic.pk,
        },
        format='json',
    )

    assert res.status_code == 201
    created = CashEntry.objects.get(pk=res.data['id'])
    assert created.tenant == salon
    assert created.amount == 150000
    assert created.payment_method == CashEntry.PaymentMethod.CASH
    assert created.recorded_by == manager
    assert res.data['recorded_by_name'] == 'r@example.com'


def test_expense_is_recorded_as_a_positive_amount(manager, salon):
    """Guaraníes are whole units and always positive; `kind` carries the sign."""
    res = api(manager, salon).post(
        LIST_URL,
        {
            'kind': 'expense',
            'amount': 45000,
            'occurred_on': '2026-03-02',
            'concept': 'Shampoo',
            'payment_method': 'transfer',
        },
        format='json',
    )

    assert res.status_code == 201
    created = CashEntry.objects.get(pk=res.data['id'])
    assert created.kind == CashEntry.Kind.EXPENSE
    assert created.amount == 45000
    assert created.payment_method == CashEntry.PaymentMethod.TRANSFER


def test_a_negative_amount_is_refused(manager, salon):
    res = api(manager, salon).post(
        LIST_URL,
        {'kind': 'expense', 'amount': -1000, 'occurred_on': '2026-03-02', 'concept': 'Oops'},
        format='json',
    )

    assert res.status_code == 400
    assert 'amount' in res.data


# --- the optional link to a booking ------------------------------------------

def test_an_entry_may_name_an_appointment_of_its_own_tenant(manager, salon):
    appointment = make_appointment(salon, user=manager)

    res = api(manager, salon).post(
        LIST_URL,
        {
            'kind': 'income',
            'amount': 150000,
            'occurred_on': '2026-03-02',
            'concept': 'Haircut',
            'appointment': str(appointment.pk),
        },
        format='json',
    )

    assert res.status_code == 201
    assert CashEntry.objects.get(pk=res.data['id']).appointment == appointment


def test_a_foreign_tenants_appointment_cannot_be_attached(manager, salon, clinic):
    """A 400, not a silent cross-tenant link: the database would accept it."""
    foreign = make_appointment(clinic, email='other@example.com')

    res = api(manager, salon).post(
        LIST_URL,
        {
            'kind': 'income',
            'amount': 150000,
            'occurred_on': '2026-03-02',
            'concept': 'Haircut',
            'appointment': str(foreign.pk),
        },
        format='json',
    )

    assert res.status_code == 400
    assert 'appointment' in res.data
    assert not CashEntry.objects.exists()


def test_deleting_the_appointment_leaves_the_money_behind(manager, salon):
    """SET_NULL: the money moved, and that stays true after the slot is gone."""
    appointment = make_appointment(salon, user=manager)
    money = CashEntry.objects.create(
        tenant=salon,
        kind=CashEntry.Kind.INCOME,
        amount=150000,
        occurred_on=date(2026, 3, 2),
        concept='Haircut',
        appointment=appointment,
    )

    appointment.delete()

    money.refresh_from_db()
    assert money.appointment is None
    assert money.amount == 150000


# --- who may touch the money -------------------------------------------------

def test_the_book_cannot_be_read_back_by_anyone(manager, salon):
    """
    Write-only on purpose: nothing in the front end reads entries back, so no
    role -- not even an admin -- gets a route to list, read, correct or
    delete them.
    """
    money = CashEntry.objects.create(
        tenant=salon, kind=CashEntry.Kind.INCOME, amount=150_000,
        occurred_on=date(2026, 8, 12), concept='Corte',
    )
    http = api(manager, salon)
    detail = reverse('accounting:cashentry-detail', args=[money.pk])

    assert http.get(LIST_URL).status_code == 405
    assert http.get(detail).status_code == 405
    assert http.patch(detail, {'amount': 1}, format='json').status_code == 405
    assert http.delete(detail).status_code == 405
    money.refresh_from_db()
    assert money.amount == 150_000


def test_staff_may_not_file_an_entry(stylist, salon):
    response = api(stylist, salon).post(LIST_URL, {
        'kind': 'income', 'amount': 50_000,
        'occurred_on': '2026-08-12', 'concept': 'Corte',
    }, format='json')

    assert response.status_code == 403
    assert not CashEntry.objects.exists()


def test_an_owner_may_file_an_entry(db, django_user_model, salon):
    """The coordinator check admits every role above it, owner included."""
    user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)

    response = api(user, salon).post(LIST_URL, {
        'kind': 'income', 'amount': 90_000,
        'occurred_on': '2026-08-20', 'concept': 'Corte',
    }, format='json')

    assert response.status_code == 201


def test_the_front_desk_may_file_an_entry(receptionist, salon):
    """
    The coordinator closes the turn and takes the payment. Letting them do the
    first and not the second would only move the till into somebody's memory.
    """
    response = api(receptionist, salon).post(LIST_URL, {
        'kind': 'income', 'amount': 90_000,
        'occurred_on': '2026-08-20', 'concept': 'Corte · Ana',
    }, format='json')

    assert response.status_code == 201
    assert CashEntry.objects.get().amount == 90_000


def test_a_coordinator_of_another_shop_may_not_file_here(db, django_user_model, salon, clinic):
    """The looser role is still bounded by the tenant. Coordinator of their own
    clinic, filing into the salon's book: the header selects, it never grants."""
    user = django_user_model.objects.create_user(email='cc@example.com', password='pw')
    Membership.objects.create(user=user, tenant=clinic, role=Membership.Role.COORDINATOR)

    response = api(user, salon).post(LIST_URL, {
        'kind': 'income', 'amount': 90_000,
        'occurred_on': '2026-08-20', 'concept': 'Corte',
    }, format='json')

    assert response.status_code == 403
    assert not CashEntry.objects.exists()
