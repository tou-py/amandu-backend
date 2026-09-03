from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership
from apps.commons.dates import one_month_after
from apps.scheduling.models import Appointment, AppointmentClient, Client, Service
from apps.tenancy.models import Tenant

LIST_URL = reverse('scheduling:client-list')


def detail_url(client):
    return reverse('scheduling:client-detail', args=[client.pk])


def api(user=None, tenant=None):
    """Authenticated caller acting for a tenant, which is how every request arrives."""
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic', country='AR')


@pytest.fixture
def receptionist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon)
    return user


def test_create_assigns_the_tenant_from_the_request(receptionist, salon):
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').tenant == salon


def test_tenant_cannot_be_set_from_the_payload(receptionist, salon, clinic):
    """editable=False keeps `tenant` out of the serializer, so this is silently ignored."""
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'tenant': clinic.pk}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').tenant == salon


def test_local_phone_is_normalised_with_the_tenant_region(receptionist, salon):
    """
    The whole point of storing E.164: typed the way a person dictates it, stored the
    way the uniqueness constraint can compare it.
    """
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'phone': '01112345678'}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').phone == '+541112345678'


def test_same_person_typed_two_ways_is_rejected_as_duplicate(receptionist, salon):
    http = api(receptionist, salon)
    http.post(LIST_URL, {'name': 'Ada', 'phone': '+54 11 1234-5678'}, format='json')

    res = http.post(LIST_URL, {'name': 'Ada Lovelace', 'phone': '01112345678'}, format='json')

    assert res.status_code == 400
    assert 'phone' in res.data


def test_a_mobile_and_its_landline_form_are_the_same_person(receptionist, salon):
    """
    E.164 storage alone does not catch this: `+54 11 2345-6789` is a Buenos Aires
    landline and `+54 9 11 2345-6789` a mobile, so both are valid, both store
    verbatim, and an equality lookup sees two unrelated strings. A receptionist
    who omits the mobile 9 was creating a second record for an existing client.
    """
    http = api(receptionist, salon)
    landline = http.post(LIST_URL, {'name': 'Ada', 'phone': '11 2345-6789'}, format='json')
    assert landline.status_code == 201
    assert Client.objects.get(name='Ada').phone == '+541123456789'

    res = http.post(
        LIST_URL, {'name': 'Ada otra vez', 'phone': '+54 9 11 2345-6789'}, format='json'
    )

    assert res.status_code == 400
    assert 'phone' in res.data
    assert Client.objects.count() == 1


def test_a_genuinely_different_number_is_still_allowed(receptionist, salon):
    """The guard must not collapse everything that ends alike into one person."""
    http = api(receptionist, salon)
    http.post(LIST_URL, {'name': 'Ada', 'phone': '11 2345-6789'}, format='json')

    res = http.post(LIST_URL, {'name': 'Grace', 'phone': '11 2345-6780'}, format='json')

    assert res.status_code == 201
    assert Client.objects.count() == 2


def test_a_client_keeps_its_own_phone_on_edit(receptionist, salon):
    """Its own row must not be read as a duplicate of itself."""
    http = api(receptionist, salon)
    created = http.post(LIST_URL, {'name': 'Ada', 'phone': '11 2345-6789'}, format='json')
    client = Client.objects.get(pk=created.data['id'])

    res = http.patch(detail_url(client), {'name': 'Ada Lovelace'}, format='json')

    assert res.status_code == 200
    assert Client.objects.get(pk=client.pk).name == 'Ada Lovelace'


def test_the_same_phone_may_exist_in_another_tenant(receptionist, salon, clinic):
    Membership.objects.create(user=receptionist, tenant=clinic)
    http = api(receptionist, salon)
    http.post(LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json')

    res = api(receptionist, clinic).post(
        LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.count() == 2


def test_many_clients_may_have_no_phone(receptionist, salon):
    """The constraint is partial: empty must not collide with empty."""
    http = api(receptionist, salon)

    first = http.post(LIST_URL, {'name': 'Ada'}, format='json')
    second = http.post(LIST_URL, {'name': 'Grace'}, format='json')

    assert (first.status_code, second.status_code) == (201, 201)


def test_invalid_phone_is_rejected(receptionist, salon):
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'phone': '123'}, format='json'
    )

    assert res.status_code == 400


def test_list_only_returns_the_active_tenants_clients(receptionist, salon, clinic):
    Client.objects.create(tenant=salon, name='Ada')
    Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).get(LIST_URL)

    assert res.status_code == 200
    assert [c['name'] for c in res.data['results']] == ['Ada']


def test_a_client_of_another_tenant_is_not_reachable_by_id(receptionist, salon, clinic):
    """Holding a valid id from another tenant must be worth nothing."""
    foreign = Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).get(detail_url(foreign))

    assert res.status_code == 404


def test_another_tenants_client_cannot_be_updated(receptionist, salon, clinic):
    foreign = Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).patch(
        detail_url(foreign), {'name': 'Hacked'}, format='json'
    )

    assert res.status_code == 404
    foreign.refresh_from_db()
    assert foreign.name == 'Grace'


def test_anonymous_gets_401_not_403(db, salon):
    res = api(tenant=salon).get(LIST_URL)

    assert res.status_code == 401


def test_authenticated_without_a_membership_gets_403(db, django_user_model, salon):
    outsider = django_user_model.objects.create_user(
        email='out@example.com', password='pw'
    )

    res = api(outsider, salon).get(LIST_URL)

    assert res.status_code == 403


def test_client_ids_are_uuid7(receptionist, salon):
    res = api(receptionist, salon).post(LIST_URL, {'name': 'Ada'}, format='json')

    assert Client.objects.get(pk=res.data['id']).id.version == 7


def test_without_a_tenant_country_a_local_number_is_rejected(db, django_user_model):
    """
    Asserts the mechanism, not just the happy outcome: with no region there is
    nothing to resolve a local format against, so the number must arrive
    international. If this ever passes, the region is coming from somewhere global
    and every tenant is being parsed as the same country.
    """
    nowhere = Tenant.objects.create(name='Nowhere', slug='nowhere')
    user = django_user_model.objects.create_user(email='n@example.com', password='pw')
    Membership.objects.create(user=user, tenant=nowhere)
    http = api(user, nowhere)

    local = http.post(LIST_URL, {'name': 'Ada', 'phone': '01112345678'}, format='json')
    international = http.post(
        LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json'
    )

    assert local.status_code == 400
    assert international.status_code == 201


# --- timeline ----------------------------------------------------------------

def timeline_url(client):
    return reverse('scheduling:client-timeline', args=[client.pk])


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


def book(salon, stylist, haircut, when, *clients, notes=''):
    appointment = Appointment.objects.create(
        tenant=salon,
        professional=stylist,
        service=haircut,
        start=when,
        end=when + haircut.duration,
        notes=notes,
    )
    for client in clients:
        AppointmentClient.objects.create(appointment=appointment, client=client)
    return appointment


def test_timeline_lists_the_clients_visits_newest_first(receptionist, salon, stylist, haircut):
    ada = Client.objects.create(tenant=salon, name='Ada')
    now = timezone.now().replace(microsecond=0)
    book(salon, stylist, haircut, now - timedelta(days=30), ada, notes='7.1 ash')
    book(salon, stylist, haircut, now + timedelta(days=2), ada)

    res = api(receptionist, salon).get(timeline_url(ada))

    assert res.status_code == 200
    visits = res.data['results']
    assert len(visits) == 2
    assert visits[0]['start'] > visits[1]['start']
    assert visits[1]['notes'] == '7.1 ash'
    assert visits[1]['service_name'] == 'Haircut'


def test_timeline_carries_this_clients_own_attendance(receptionist, salon, stylist, haircut):
    """A slot holding a group has one answer per person, not one for the booking."""
    ada = Client.objects.create(tenant=salon, name='Ada')
    grace = Client.objects.create(tenant=salon, name='Grace')
    appointment = book(salon, stylist, haircut, timezone.now() - timedelta(days=1), ada, grace)
    appointment.client_links.filter(client=grace).update(attendance='no_show')

    res = api(receptionist, salon).get(timeline_url(ada))

    assert [v['attendance'] for v in res.data['results']] == ['pending']


def test_timeline_shows_only_that_clients_visits(receptionist, salon, stylist, haircut):
    ada = Client.objects.create(tenant=salon, name='Ada')
    grace = Client.objects.create(tenant=salon, name='Grace')
    book(salon, stylist, haircut, timezone.now() - timedelta(days=1), grace)

    res = api(receptionist, salon).get(timeline_url(ada))

    assert res.data['results'] == []


def test_timeline_of_another_tenants_client_is_not_reachable(receptionist, salon, clinic):
    foreign = Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).get(timeline_url(foreign))

    assert res.status_code == 404


def payment_url(client):
    return reverse('scheduling:client-register-payment', args=[client.pk])


def test_a_client_without_paid_until_has_no_plan_at_all(salon):
    """
    NULL is 'pays per session' here, the opposite of Tenant.paid_until, where it
    means 'never expires'. Inverting it would grant a standing subscription to
    every client who never bought one.
    """
    ada = Client.objects.create(tenant=salon, name='Ada')

    assert ada.plan_state() == 'none'


def test_a_client_paid_through_today_is_still_active(salon):
    """The boundary. `paid_until` is a day the operator names, not an instant, so
    the last day it covers is covered in full."""
    ada = Client.objects.create(
        tenant=salon, name='Ada', monthly_fee=300000, paid_until=timezone.localdate()
    )

    assert ada.plan_state() == 'active'


def test_a_client_whose_last_paid_day_has_passed_is_expired(salon):
    ada = Client.objects.create(
        tenant=salon,
        name='Ada',
        monthly_fee=300000,
        paid_until=timezone.localdate() - timedelta(days=1),
    )

    assert ada.plan_state() == 'expired'


def test_the_client_payload_carries_the_plan_and_the_state_derived_from_it(receptionist, salon):
    paid_until = timezone.localdate() + timedelta(days=5)
    ada = Client.objects.create(
        tenant=salon, name='Ada', monthly_fee=300000, paid_until=paid_until
    )

    res = api(receptionist, salon).get(detail_url(ada))

    assert res.status_code == 200
    assert res.data['monthly_fee'] == 300000
    assert res.data['paid_until'] == paid_until.isoformat()
    assert res.data['plan_state'] == 'active'


def test_a_plan_can_be_given_and_taken_away_through_the_api(receptionist, salon):
    """A studio sells plans, a salon sells none: both shapes must round-trip."""
    http = api(receptionist, salon)
    res = http.post(
        LIST_URL,
        {'name': 'Ada', 'monthly_fee': 300000, 'paid_until': '2026-12-31'},
        format='json',
    )

    assert res.status_code == 201
    assert res.data['plan_state'] == 'active'

    cleared = http.patch(
        detail_url(Client.objects.get(name='Ada')),
        {'monthly_fee': None, 'paid_until': None},
        format='json',
    )

    assert cleared.status_code == 200
    assert cleared.data['plan_state'] == 'none'


def test_registering_a_payment_on_an_active_plan_extends_from_its_own_end(receptionist, salon):
    """Paying early stacks onto what is left instead of throwing it away."""
    remaining = timezone.localdate() + timedelta(days=10)
    ada = Client.objects.create(
        tenant=salon, name='Ada', monthly_fee=300000, paid_until=remaining
    )

    res = api(receptionist, salon).post(payment_url(ada), format='json')

    assert res.status_code == 200
    ada.refresh_from_db()
    assert ada.paid_until == one_month_after(remaining)


def test_registering_a_payment_on_an_expired_plan_extends_from_today(receptionist, salon):
    """Paying late buys a month from now: nobody owes us the days they spent uncovered."""
    today = timezone.localdate()
    ada = Client.objects.create(
        tenant=salon, name='Ada', monthly_fee=300000, paid_until=today - timedelta(days=40)
    )

    res = api(receptionist, salon).post(payment_url(ada), format='json')

    assert res.status_code == 200
    ada.refresh_from_db()
    assert ada.paid_until == one_month_after(today)
    assert res.data['plan_state'] == 'active'


def test_registering_a_payment_books_the_money_in_the_cash_book(receptionist, salon):
    ada = Client.objects.create(tenant=salon, name='Ada', monthly_fee=300000)

    res = api(receptionist, salon).post(payment_url(ada), format='json')

    assert res.status_code == 200
    ada.refresh_from_db()
    entry = CashEntry.objects.get()
    assert entry.tenant == salon
    assert entry.kind == CashEntry.Kind.INCOME
    assert entry.amount == 300000
    assert entry.occurred_on == timezone.localdate()
    assert entry.appointment is None
    # Pinned wording, not a loose contains: this line is read by the shop in its
    # cash book, sitting between entries the front end writes in Spanish, so the
    # language is part of the behaviour rather than an implementation detail.
    assert entry.concept == f'Mensualidad · Ada · hasta {ada.paid_until:%d/%m/%Y}'


def test_a_client_with_no_monthly_fee_cannot_be_charged(receptionist, salon):
    """There is no amount to take, and recording a zero would corrupt the day's takings."""
    ada = Client.objects.create(tenant=salon, name='Ada')

    res = api(receptionist, salon).post(payment_url(ada), format='json')

    assert res.status_code == 400
    ada.refresh_from_db()
    assert ada.paid_until is None
    assert not CashEntry.objects.exists()


def test_a_payment_cannot_be_registered_for_another_tenants_client(receptionist, salon, clinic):
    foreign = Client.objects.create(tenant=clinic, name='Grace', monthly_fee=300000)

    res = api(receptionist, salon).post(payment_url(foreign), format='json')

    assert res.status_code == 404
    assert not CashEntry.objects.exists()
