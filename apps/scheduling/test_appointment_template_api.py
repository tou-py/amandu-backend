from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, AppointmentTemplate, Client, Service
from apps.tenancy.models import Tenant

LIST_URL = reverse('scheduling:appointment-template-list')
# A Monday, far enough in the future that nothing else in the suite collides
# with it; the salon fixture's tenant timezone is the model default (UTC), so
# this date is also the UTC calendar day the generated appointments land on.
MONDAY = date(2026, 8, 3)


def detail_url(template):
    return reverse('scheduling:appointment-template-detail', args=[template.pk])


def generate_url(template):
    return reverse('scheduling:appointment-template-generate', args=[template.pk])


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
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def client_(db, salon):
    return Client.objects.create(tenant=salon, name='Ada')


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


def payload(professional, clients, service, **overrides):
    body = {
        'professional': professional.pk,
        'service': service.pk,
        'clients': [c.pk for c in clients],
        'name': 'Spin lunes',
        'start_date': MONDAY.isoformat(),
        'start_time': '10:00:00',
    }
    body.update(overrides)
    return body


def test_creating_a_template(receptionist, salon, stylist, client_, haircut):
    res = api(receptionist, salon).post(
        LIST_URL, payload(stylist, [client_], haircut, weekdays=[0]), format='json'
    )

    assert res.status_code == 201
    template = AppointmentTemplate.objects.get(pk=res.data['id'])
    assert template.tenant == salon
    assert list(template.clients.all()) == [client_]
    assert template.status == AppointmentTemplate.Status.ACTIVE


def test_generate_respects_the_weekdays_rule(salon, stylist, client_, haircut):
    """A weekdays list of [Mon, Wed, Fri] is the one mechanism that covers both
    a single weekly slot and a multi-day-per-week one."""
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Spin',
        start_date=MONDAY, start_time=time(10, 0), weekdays=[0, 2, 4],
    )
    template.clients.add(client_)

    res = api(stylist.user, salon).post(generate_url(template), {'count': 3}, format='json')

    assert res.status_code == 200
    assert len(res.data['created']) == 3
    assert res.data['skipped'] == []
    weekdays = sorted(
        a.start.weekday() for a in Appointment.objects.filter(template=template)
    )
    assert weekdays == [0, 2, 4]


def test_generate_respects_the_interval_days_rule(salon, stylist, client_, haircut):
    """No weekdays set -> plain every-N-days, daily being the N=1 case."""
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Every 3 days',
        start_date=MONDAY, start_time=time(9, 0), interval_days=3,
    )
    template.clients.add(client_)

    res = api(stylist.user, salon).post(generate_url(template), {'count': 3}, format='json')

    assert res.status_code == 200
    dates = sorted(a.start.date() for a in Appointment.objects.filter(template=template))
    assert dates == [MONDAY, MONDAY + timedelta(days=3), MONDAY + timedelta(days=6)]


def test_generation_stops_at_max_occurrences(salon, stylist, client_, haircut):
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Daily',
        start_date=MONDAY, start_time=time(9, 0), max_occurrences=2,
    )
    template.clients.add(client_)

    res = api(stylist.user, salon).post(generate_url(template), {'count': 5}, format='json')

    assert res.status_code == 200
    assert len(res.data['created']) == 2
    assert Appointment.objects.filter(template=template).count() == 2


def test_generation_stops_at_end_date(salon, stylist, client_, haircut):
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Daily',
        start_date=MONDAY, start_time=time(9, 0), end_date=MONDAY + timedelta(days=1),
    )
    template.clients.add(client_)

    res = api(stylist.user, salon).post(generate_url(template), {'count': 5}, format='json')

    assert res.status_code == 200
    assert len(res.data['created']) == 2


def test_a_colliding_date_is_skipped_but_the_rest_of_the_batch_still_generates(
    salon, stylist, client_, haircut
):
    """The transaction-isolation guarantee this design exists for: one
    occurrence's ExclusionConstraint violation must not poison the others."""
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Daily',
        start_date=MONDAY, start_time=time(9, 0),
    )
    template.clients.add(client_)
    colliding_day = MONDAY + timedelta(days=1)
    # The salon fixture leaves Tenant.timezone at its model default (UTC), so
    # this is exactly the instant `generate_occurrences` will try to book.
    conflict_start = datetime.combine(colliding_day, time(9, 0), tzinfo=ZoneInfo('UTC'))
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=conflict_start, end=conflict_start + haircut.duration,
    )

    res = api(stylist.user, salon).post(generate_url(template), {'count': 3}, format='json')

    assert res.status_code == 200
    assert len(res.data['created']) == 2
    assert res.data['skipped'] == [{'date': colliding_day.isoformat(), 'reason': 'conflict'}]
    created_dates = sorted(a.start.date() for a in Appointment.objects.filter(template=template))
    assert created_dates == [MONDAY, MONDAY + timedelta(days=2)]


def test_a_generate_call_above_the_cap_is_rejected(salon, stylist, client_, haircut):
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Daily',
        start_date=MONDAY, start_time=time(9, 0),
    )
    template.clients.add(client_)

    res = api(stylist.user, salon).post(generate_url(template), {'count': 13}, format='json')

    assert res.status_code == 400
    assert not Appointment.objects.filter(template=template).exists()


def test_staff_can_only_template_for_self(
    db, django_user_model, salon, stylist, client_, haircut
):
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='other@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )

    res = api(other.user, salon).post(
        LIST_URL, payload(stylist, [client_], haircut), format='json'
    )

    assert res.status_code == 400
    assert 'professional' in res.data
    assert not AppointmentTemplate.objects.exists()


def test_a_coordinator_may_template_for_the_whole_team(receptionist, salon, stylist, client_, haircut):
    res = api(receptionist, salon).post(
        LIST_URL, payload(stylist, [client_], haircut), format='json'
    )

    assert res.status_code == 201


def test_auto_generate_on_complete_creates_exactly_one_occurrence_when_enabled(
    salon, stylist, client_, haircut
):
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Daily',
        start_date=MONDAY, start_time=time(9, 0), auto_generate_on_complete=True,
    )
    template.clients.add(client_)
    past = timezone.now() - timedelta(hours=2)
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=past, end=past + timedelta(minutes=30), template=template,
    )
    appointment.clients.add(client_)

    res = api(stylist.user, salon).post(
        reverse('scheduling:appointment-complete', args=[appointment.pk])
    )

    assert res.status_code == 200
    assert Appointment.objects.filter(template=template).count() == 2


def test_auto_generate_on_complete_creates_nothing_when_disabled(
    salon, stylist, client_, haircut
):
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Daily',
        start_date=MONDAY, start_time=time(9, 0), auto_generate_on_complete=False,
    )
    template.clients.add(client_)
    past = timezone.now() - timedelta(hours=2)
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=past, end=past + timedelta(minutes=30), template=template,
    )
    appointment.clients.add(client_)

    res = api(stylist.user, salon).post(
        reverse('scheduling:appointment-complete', args=[appointment.pk])
    )

    assert res.status_code == 200
    assert Appointment.objects.filter(template=template).count() == 1


def test_template_name_appears_on_the_appointment_it_generated(salon, stylist, client_, haircut):
    template = AppointmentTemplate.objects.create(
        tenant=salon, professional=stylist, service=haircut, name='Spin lunes 10am',
        start_date=MONDAY, start_time=time(9, 0),
    )
    template.clients.add(client_)

    res = api(stylist.user, salon).post(generate_url(template), {'count': 1}, format='json')

    assert res.data['created'][0]['template_name'] == 'Spin lunes 10am'


def test_template_name_is_null_for_an_untemplated_appointment(receptionist, salon, stylist, client_, haircut):
    res = api(receptionist, salon).post(
        reverse('scheduling:appointment-list'),
        {
            'professional': stylist.pk, 'clients': [client_.pk], 'service': haircut.pk,
            'start': (timezone.now() + timedelta(days=1)).isoformat(),
        },
        format='json',
    )

    assert res.data['template_name'] is None
