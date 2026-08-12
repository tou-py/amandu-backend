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
SUMMARY_URL = reverse('accounting:cashentry-summary')


def detail_url(entry):
    return reverse('accounting:cashentry-detail', args=[entry.pk])


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
def intruder(db, django_user_model, clinic):
    """
    Active membership in the OTHER tenant: a real caller, wrong shop.

    Admin of their own clinic on purpose. Left as staff, the role check would
    refuse them before the tenant scoping was ever consulted, and the boundary
    tests below would pass without testing the boundary. Somebody with every
    right to read their OWN book is exactly who must not reach this one.
    """
    user = django_user_model.objects.create_user(email='i@example.com', password='pw')
    Membership.objects.create(user=user, tenant=clinic, role=Membership.Role.ADMIN)
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


def entry(tenant, kind, amount, occurred_on, concept='Whatever'):
    return CashEntry.objects.create(
        tenant=tenant,
        kind=kind,
        amount=amount,
        occurred_on=occurred_on,
        concept=concept,
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


# --- the tenant boundary -----------------------------------------------------

def test_the_list_only_shows_the_active_tenants_entries(manager, salon, clinic):
    entry(salon, CashEntry.Kind.INCOME, 150000, date(2026, 3, 2), 'Ours')
    entry(clinic, CashEntry.Kind.INCOME, 999000, date(2026, 3, 2), 'Theirs')

    res = api(manager, salon).get(LIST_URL)

    assert res.status_code == 200
    assert [e['concept'] for e in res.data['results']] == ['Ours']


def test_another_tenants_entry_is_not_reachable_by_id(intruder, salon, clinic):
    """
    The important one. `intruder` is a fully authenticated caller with a live
    membership -- just in the wrong shop -- so nothing but the tenant scoping
    stands between them and this row. A 404 and not a 403: the entry does not
    exist as far as this tenant is concerned, and a 403 would confirm it does.
    """
    foreign = entry(salon, CashEntry.Kind.INCOME, 150000, date(2026, 3, 2), 'Salon takings')

    http = api(intruder, clinic)
    assert http.get(detail_url(foreign)).status_code == 404
    assert http.patch(detail_url(foreign), {'amount': 1}, format='json').status_code == 404
    assert http.delete(detail_url(foreign)).status_code == 404

    foreign.refresh_from_db()
    assert foreign.amount == 150000


def test_another_tenants_entries_do_not_reach_the_summary(intruder, salon, clinic):
    """Scoping the detail route is not enough if the aggregate reads everyone."""
    entry(salon, CashEntry.Kind.INCOME, 150000, date(2026, 3, 2))

    res = api(intruder, clinic).get(SUMMARY_URL)

    assert res.status_code == 200
    assert res.data == {'income': 0, 'expense': 0, 'balance': 0}


# --- filtering ---------------------------------------------------------------

def test_from_and_to_bound_the_range_at_both_ends(manager, salon):
    """Both ends inclusive: an entry ON the boundary day is inside the range."""
    entry(salon, CashEntry.Kind.INCOME, 1000, date(2026, 3, 1), 'Before')
    entry(salon, CashEntry.Kind.INCOME, 2000, date(2026, 3, 2), 'First day')
    entry(salon, CashEntry.Kind.INCOME, 3000, date(2026, 3, 4), 'Last day')
    entry(salon, CashEntry.Kind.INCOME, 4000, date(2026, 3, 5), 'After')

    res = api(manager, salon).get(LIST_URL, {'from': '2026-03-02', 'to': '2026-03-04'})

    assert res.status_code == 200
    assert {e['concept'] for e in res.data['results']} == {'First day', 'Last day'}


def test_kind_filters_the_list(manager, salon):
    entry(salon, CashEntry.Kind.INCOME, 1000, date(2026, 3, 2), 'Takings')
    entry(salon, CashEntry.Kind.EXPENSE, 500, date(2026, 3, 2), 'Shampoo')

    res = api(manager, salon).get(LIST_URL, {'kind': 'expense'})

    assert res.status_code == 200
    assert [e['concept'] for e in res.data['results']] == ['Shampoo']


def test_a_malformed_date_is_a_400_not_a_500(manager, salon):
    res = api(manager, salon).get(LIST_URL, {'from': '02/03/2026'})

    assert res.status_code == 400
    assert 'from' in res.data


def test_an_unknown_kind_is_a_400(manager, salon):
    res = api(manager, salon).get(LIST_URL, {'kind': 'refund'})

    assert res.status_code == 400
    assert 'kind' in res.data


def test_the_book_reads_most_recent_business_day_first(manager, salon):
    entry(salon, CashEntry.Kind.INCOME, 1000, date(2026, 3, 1), 'Sunday')
    entry(salon, CashEntry.Kind.INCOME, 2000, date(2026, 3, 3), 'Tuesday')
    entry(salon, CashEntry.Kind.INCOME, 3000, date(2026, 3, 2), 'Monday')

    res = api(manager, salon).get(LIST_URL)

    assert [e['concept'] for e in res.data['results']] == ['Tuesday', 'Monday', 'Sunday']


# --- the number the owner opens the app for ----------------------------------

def test_summary_adds_up_income_expense_and_balance(manager, salon):
    entry(salon, CashEntry.Kind.INCOME, 150000, date(2026, 3, 2))
    entry(salon, CashEntry.Kind.INCOME, 80000, date(2026, 3, 3))
    entry(salon, CashEntry.Kind.EXPENSE, 45000, date(2026, 3, 3))

    res = api(manager, salon).get(SUMMARY_URL)

    assert res.status_code == 200
    assert res.data == {'income': 230000, 'expense': 45000, 'balance': 185000}


def test_summary_honours_the_same_range_as_the_list(manager, salon):
    entry(salon, CashEntry.Kind.INCOME, 999000, date(2026, 2, 28), 'Last month')
    entry(salon, CashEntry.Kind.INCOME, 150000, date(2026, 3, 2))
    entry(salon, CashEntry.Kind.EXPENSE, 45000, date(2026, 3, 2))

    res = api(manager, salon).get(SUMMARY_URL, {'from': '2026-03-01', 'to': '2026-03-31'})

    assert res.data == {'income': 150000, 'expense': 45000, 'balance': 105000}


def test_an_empty_range_is_zeros_and_never_nulls(manager, salon):
    """A day that made nothing is a real answer. A null would make every caller
    guess whether it meant zero or 'no idea'."""
    entry(salon, CashEntry.Kind.INCOME, 150000, date(2026, 3, 2))

    res = api(manager, salon).get(SUMMARY_URL, {'from': '2026-04-01', 'to': '2026-04-30'})

    assert res.status_code == 200
    assert res.data == {'income': 0, 'expense': 0, 'balance': 0}


def test_a_losing_range_gives_a_negative_balance(manager, salon):
    """The amounts are unsigned; the balance is not."""
    entry(salon, CashEntry.Kind.INCOME, 10000, date(2026, 3, 2))
    entry(salon, CashEntry.Kind.EXPENSE, 90000, date(2026, 3, 2))

    res = api(manager, salon).get(SUMMARY_URL)

    assert res.data['balance'] == -80000


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


# --- who may look at the money -----------------------------------------------

def test_staff_may_not_read_the_book(manager, stylist, salon):
    """
    Reads are refused too, not just writes.

    Unlike the schedule, where staff read the hours they work and only an admin
    changes them, the protected thing here IS the figures. A stylist opening the
    tab must not be handed what the shop bills.
    """
    CashEntry.objects.create(
        tenant=salon, kind=CashEntry.Kind.INCOME, amount=150_000,
        occurred_on=date(2026, 8, 12), concept='Corte',
    )

    assert api(stylist, salon).get(LIST_URL).status_code == 403


def test_staff_may_not_read_the_summary(stylist, salon):
    """The totals are the whole point of the restriction: refusing the list and
    serving the aggregate would hand over the number that matters most."""
    assert api(stylist, salon).get(SUMMARY_URL).status_code == 403


def test_staff_may_not_file_an_entry(stylist, salon):
    response = api(stylist, salon).post(LIST_URL, {
        'kind': 'income', 'amount': 50_000,
        'occurred_on': '2026-08-12', 'concept': 'Corte',
    }, format='json')

    assert response.status_code == 403
    assert not CashEntry.objects.exists()


def test_staff_may_not_touch_an_existing_entry(stylist, salon):
    entry = CashEntry.objects.create(
        tenant=salon, kind=CashEntry.Kind.EXPENSE, amount=80_000,
        occurred_on=date(2026, 8, 12), concept='Shampoo',
    )
    http = api(stylist, salon)

    assert http.get(detail_url(entry)).status_code == 403
    assert http.patch(detail_url(entry), {'amount': 1}, format='json').status_code == 403
    assert http.delete(detail_url(entry)).status_code == 403
    entry.refresh_from_db()
    assert entry.amount == 80_000


def test_an_owner_is_not_shut_out_by_the_admin_check(db, django_user_model, salon):
    """IsTenantAdmin admits owner AND admin. A guard that only let admins in
    would lock out the one person the shop belongs to."""
    user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)

    assert api(user, salon).get(LIST_URL).status_code == 200
