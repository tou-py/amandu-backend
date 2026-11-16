from datetime import timedelta

import pytest
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Client, Service
from apps.tenancy.models import Tenant

LIST_URL = reverse('scheduling:professional-list')
TOMORROW = timezone.now().replace(microsecond=0) + timedelta(days=1)


def api(user=None, tenant=None):
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
def receptionist(db, django_user_model, salon):
    """Acts for the tenant but is never booked (attends_appointments default False)."""
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon)
    return user


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(
        email='s@example.com', password='pw', first_name='Ada', last_name='Lovelace'
    )
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


def test_lists_only_bookable_members_of_the_caller_tenant(receptionist, salon, stylist):
    """The receptionist has access to the agenda but never appears in it, and a
    stylist from another tenant must not leak in."""
    other = Tenant.objects.create(name='Clinic', slug='clinic', country='AR')
    outsider = type(receptionist).objects.create_user(email='o@example.com', password='pw')
    Membership.objects.create(user=outsider, tenant=other, attends_appointments=True)

    res = api(receptionist, salon).get(LIST_URL)

    assert res.status_code == 200
    assert [row['id'] for row in res.data] == [stylist.pk]
    assert res.data[0]['name'] == 'Ada Lovelace'


def test_a_member_without_a_name_is_labelled_by_email(receptionist, salon, stylist):
    stylist.user.first_name = ''
    stylist.user.last_name = ''
    stylist.user.save(update_fields=['first_name', 'last_name'])

    res = api(receptionist, salon).get(LIST_URL)

    assert res.data[0]['name'] == 's@example.com'


def test_a_suspended_member_is_not_bookable(receptionist, salon, stylist):
    stylist.status = Membership.Status.SUSPENDED
    stylist.save(update_fields=['status'])

    res = api(receptionist, salon).get(LIST_URL)

    assert res.data == []


def test_the_list_is_not_paginated(receptionist, salon, stylist):
    """A truncated page would silently mislabel every slot of the professionals
    left on page two, so the response is a bare list, not an envelope."""
    res = api(receptionist, salon).get(LIST_URL)

    assert isinstance(res.data, list)


def test_appointment_labels_do_not_scale_queries_with_rows(
    receptionist, salon, stylist, django_user_model
):
    """The agenda serializes a name for the professional, the service and every
    client. Comparing two list calls instead of asserting a fixed number keeps
    this honest about N+1 without breaking on unrelated query changes."""
    haircut = Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))
    ada = Client.objects.create(tenant=salon, name='Ada')
    http = api(receptionist, salon)
    url = reverse('scheduling:appointment-list')

    def book(professional, offset):
        appointment = Appointment.objects.create(
            tenant=salon,
            professional=professional,
            service=haircut,
            start=TOMORROW + timedelta(hours=offset),
            end=TOMORROW + timedelta(hours=offset, minutes=30),
        )
        appointment.clients.add(ada)

    book(stylist, 0)
    with CaptureQueriesContext(connection) as one_row:
        assert http.get(url).status_code == 200

    # A second professional, so the added rows do not all share one prefetch key.
    other_user = django_user_model.objects.create_user(email='b@example.com', password='pw')
    other = Membership.objects.create(user=other_user, tenant=salon, attends_appointments=True)
    book(other, 1)
    book(stylist, 2)
    with CaptureQueriesContext(connection) as three_rows:
        assert http.get(url).status_code == 200

    assert len(three_rows) == len(one_row)
