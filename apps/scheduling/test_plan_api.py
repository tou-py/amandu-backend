from datetime import timedelta

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.commons.dates import add_months
from apps.scheduling.billing import local_today
from apps.scheduling.models import Category, Client, Plan, Subscription
from apps.tenancy.models import Tenant

PLANS = reverse('scheduling:plan-list')
SUBSCRIPTIONS = reverse('scheduling:subscription-list')
DEFAULT_START = reverse('scheduling:subscription-default-start')


def plan_url(plan):
    return reverse('scheduling:plan-detail', args=[plan.pk])


def sub_url(subscription, name):
    return reverse(f'scheduling:subscription-{name}', args=[subscription.pk])


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def studio(db):
    return Tenant.objects.create(name='Studio', slug='studio', country='PY')


@pytest.fixture
def gym(db):
    return Tenant.objects.create(name='Gym', slug='gym', country='PY')


def member(django_user_model, tenant, role, email):
    user = django_user_model.objects.create_user(email=email, password='pw')
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return user


@pytest.fixture
def owner(db, django_user_model, studio):
    return member(django_user_model, studio, Membership.Role.OWNER, 'o@example.com')


@pytest.fixture
def desk(db, django_user_model, studio):
    """Front desk: subscribes clients, does not price the catalogue."""
    return member(django_user_model, studio, Membership.Role.COORDINATOR, 'c@example.com')


@pytest.fixture
def pilates(studio):
    return Plan.objects.create(tenant=studio, name='Pilates 8', price=250000, sessions_per_period=8)


@pytest.fixture
def ada(studio):
    return Client.objects.create(tenant=studio, name='Ada')


# -- The catalogue -------------------------------------------------------------


def test_an_owner_creates_a_plan_restricted_to_some_categories(owner, studio):
    mat = Category.objects.create(tenant=studio, name='Pilates')

    res = api(owner, studio).post(
        PLANS,
        {'name': 'Pilates 8', 'price': 250000, 'sessions_per_period': 8, 'categories': [mat.pk]},
        format='json',
    )

    assert res.status_code == 201
    assert res.data['price'] == 250000
    assert res.data['categories'] == [mat.pk]
    assert res.data['archived'] is False


def test_a_plan_with_no_sessions_is_unlimited(owner, studio):
    res = api(owner, studio).post(PLANS, {'name': 'Libre', 'price': 300000}, format='json')

    assert res.status_code == 201
    assert res.data['sessions_per_period'] is None
    assert res.data['categories'] == []


@pytest.mark.parametrize('role', [Membership.Role.COORDINATOR, Membership.Role.STAFF])
def test_only_owner_or_admin_writes_the_catalogue_but_anyone_reads_it(
    django_user_model, studio, pilates, role
):
    caller = member(django_user_model, studio, role, 'x@example.com')
    http = api(caller, studio)

    res = http.post(PLANS, {'name': 'Libre', 'price': 1}, format='json')
    assert (res.status_code, res.data['code']) == (403, 'admin_required')
    assert http.patch(plan_url(pilates), {'price': 1}, format='json').status_code == 403
    listed = http.get(PLANS)
    assert listed.status_code == 200
    assert [p['name'] for p in listed.data] == ['Pilates 8']


def test_archiving_keeps_the_plan_and_its_subscriptions(owner, studio, pilates, ada):
    Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date=local_today(studio))

    res = api(owner, studio).patch(plan_url(pilates), {'archived': True}, format='json')

    assert res.status_code == 200
    assert res.data['archived'] is True
    assert pilates.subscriptions.count() == 1


def test_a_plan_cannot_cover_another_tenants_category(owner, studio, gym):
    foreign = Category.objects.create(tenant=gym, name='Spinning')

    res = api(owner, studio).post(
        PLANS, {'name': 'Mixto', 'price': 1, 'categories': [foreign.pk]}, format='json'
    )

    assert (res.status_code, res.data['code']) == (400, 'does_not_exist')


def test_plans_are_isolated_per_tenant(django_user_model, owner, studio, gym, pilates):
    Plan.objects.create(tenant=gym, name='Spinning', price=1)
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')

    assert [p['name'] for p in api(owner, studio).get(PLANS).data] == ['Pilates 8']
    assert api(outsider, gym).get(plan_url(pilates)).status_code == 404
    assert api(outsider, gym).patch(plan_url(pilates), {'price': 1}, format='json').status_code == 404


def test_two_plans_cannot_share_a_name(owner, studio, pilates):
    res = api(owner, studio).post(PLANS, {'name': 'pilates 8', 'price': 1}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'name_taken')


# -- Business settings ---------------------------------------------------------


def test_the_owner_sets_the_period_start_and_the_grace_days(owner, studio):
    url = reverse('tenancy:tenant')

    res = api(owner, studio).patch(
        url, {'plan_period_start': 'join_day', 'plan_grace_days': 5}, format='json'
    )

    assert res.status_code == 200
    assert res.data['plan_period_start'] == 'join_day'
    assert res.data['plan_grace_days'] == 5


def test_grace_days_stop_at_28(owner, studio):
    res = api(owner, studio).patch(reverse('tenancy:tenant'), {'plan_grace_days': 29}, format='json')

    assert res.status_code == 400


def test_a_new_tenant_collects_by_the_10th(studio):
    assert studio.plan_period_start == 'month_start'
    assert studio.plan_grace_days == 9


# -- Subscribing ---------------------------------------------------------------


def test_month_start_prefills_the_next_first(desk, studio, pilates, ada):
    today = local_today(studio)
    expected = today if today.day == 1 else add_months(today.replace(day=1), 1)

    res = api(desk, studio).post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk}, format='json')

    assert res.status_code == 201
    assert res.data['start_date'] == expected.isoformat()
    assert api(desk, studio).get(DEFAULT_START).data == {'start_date': expected.isoformat()}


def test_join_day_prefills_today(desk, studio, pilates, ada):
    studio.plan_period_start = Tenant.PlanPeriodStart.JOIN_DAY
    studio.save()

    res = api(desk, studio).post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk}, format='json')

    assert res.data['start_date'] == local_today(studio).isoformat()


def test_front_desk_subscribes_with_its_own_start_date_and_price(desk, studio, pilates, ada):
    res = api(desk, studio).post(
        SUBSCRIPTIONS,
        {'client': ada.pk, 'plan': pilates.pk, 'start_date': '2026-08-25', 'price_override': 200000},
        format='json',
    )

    assert res.status_code == 201
    assert res.data['start_date'] == '2026-08-25'
    assert res.data['price'] == 200000
    assert res.data['plan_name'] == 'Pilates 8'
    assert res.data['end_date'] is None


def test_the_effective_price_follows_the_plan_without_an_override(desk, studio, pilates, ada):
    res = api(desk, studio).post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk}, format='json')

    assert res.data['price'] == 250000
    assert res.data['price_override'] is None


def test_a_client_has_at_most_one_open_subscription(desk, studio, pilates, ada):
    http = api(desk, studio)
    http.post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk}, format='json')

    res = http.post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'subscription_overlap')
    assert ada.subscriptions.count() == 1


def test_an_overlapping_closed_range_is_rejected(desk, studio, pilates, ada):
    Subscription.objects.create(
        tenant=studio, client=ada, plan=pilates,
        start_date='2026-01-01', end_date='2026-03-31',
    )
    http = api(desk, studio)

    clash = http.post(
        SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk, 'start_date': '2026-03-31'}, format='json'
    )
    after = http.post(
        SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk, 'start_date': '2026-04-01'}, format='json'
    )

    assert (clash.status_code, clash.data['code']) == (400, 'subscription_overlap')
    assert after.status_code == 201


def test_an_archived_plan_cannot_be_picked(desk, studio, pilates, ada):
    pilates.archived = True
    pilates.save()

    res = api(desk, studio).post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'plan_archived')


def test_subscriptions_are_listed_per_client(desk, studio, pilates, ada):
    other = Client.objects.create(tenant=studio, name='Bob')
    Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-01')
    Subscription.objects.create(tenant=studio, client=other, plan=pilates, start_date='2026-01-01')

    res = api(desk, studio).get(SUBSCRIPTIONS, {'client': ada.pk})

    assert res.status_code == 200
    assert [s['client'] for s in res.data] == [ada.pk]


def test_subscriptions_are_isolated_per_tenant(django_user_model, desk, studio, gym, pilates, ada):
    foreign_client = Client.objects.create(tenant=gym, name='Grace')
    foreign_plan = Plan.objects.create(tenant=gym, name='Spinning', price=1)
    mine = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-01')
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')
    http = api(desk, studio)

    assert http.post(
        SUBSCRIPTIONS, {'client': foreign_client.pk, 'plan': pilates.pk}, format='json'
    ).status_code == 400
    assert http.post(
        SUBSCRIPTIONS, {'client': ada.pk, 'plan': foreign_plan.pk}, format='json'
    ).status_code == 400
    assert api(outsider, gym).get(SUBSCRIPTIONS).data == []
    assert api(outsider, gym).post(sub_url(mine, 'end'), {}, format='json').status_code == 404
    assert api(outsider, gym).post(
        sub_url(mine, 'change-plan'), {'plan': foreign_plan.pk}, format='json'
    ).status_code == 404


# -- Ending --------------------------------------------------------------------


def test_ending_defaults_to_the_last_day_of_the_last_paid_period(desk, studio, pilates, ada):
    """A client who paid January and February and stopped coming: ending the
    lapsed plan must not invent March to December."""
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')
    for period in ('2026-01-14', '2026-02-14'):
        sub.cash_entries.create(
            tenant=studio, kind='income', amount=1, occurred_on=period, concept='x',
            client=ada, period=period,
        )

    res = api(desk, studio).post(sub_url(sub, 'end'), {}, format='json')

    assert res.status_code == 200
    assert res.data['end_date'] == '2026-03-13'


def test_a_voided_payment_does_not_extend_the_default_end(desk, studio, pilates, ada):
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')
    sub.cash_entries.create(
        tenant=studio, kind='income', amount=1, occurred_on='2026-01-14', concept='x',
        client=ada, period='2026-01-14',
    )
    sub.cash_entries.create(
        tenant=studio, kind='income', amount=1, occurred_on='2026-02-14', concept='x',
        client=ada, period='2026-02-14', voided_at='2026-02-15T10:00Z',
    )

    res = api(desk, studio).post(sub_url(sub, 'end'), {}, format='json')

    assert res.data['end_date'] == '2026-02-13'


def test_ending_with_a_chosen_date(desk, studio, pilates, ada):
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')

    res = api(desk, studio).post(sub_url(sub, 'end'), {'end_date': '2026-08-31'}, format='json')

    assert res.data['end_date'] == '2026-08-31'


def test_a_never_paid_subscription_needs_an_explicit_end_date(desk, studio, pilates, ada):
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')

    res = api(desk, studio).post(sub_url(sub, 'end'), {}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'end_date_required')


def test_a_subscription_cannot_end_before_it_starts(desk, studio, pilates, ada):
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')

    res = api(desk, studio).post(sub_url(sub, 'end'), {'end_date': '2026-01-13'}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'end_before_start')


def test_an_ended_subscription_cannot_end_or_change_plan_again(desk, studio, pilates, ada):
    sub = Subscription.objects.create(
        tenant=studio, client=ada, plan=pilates, start_date='2026-01-14', end_date='2026-02-13'
    )
    http = api(desk, studio)

    ended = http.post(sub_url(sub, 'end'), {'end_date': '2026-03-13'}, format='json')
    moved = http.post(sub_url(sub, 'change-plan'), {'plan': pilates.pk}, format='json')

    assert (ended.status_code, ended.data['code']) == (409, 'subscription_ended')
    assert (moved.status_code, moved.data['code']) == (409, 'subscription_ended')


def test_ending_frees_the_client_for_a_new_subscription(desk, studio, pilates, ada):
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')
    http = api(desk, studio)
    http.post(sub_url(sub, 'end'), {'end_date': '2026-05-13'}, format='json')

    res = http.post(
        SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk, 'start_date': '2026-05-14'}, format='json'
    )

    assert res.status_code == 201


# -- Changing plan -------------------------------------------------------------


def test_changing_plan_takes_effect_from_the_next_period_and_keeps_the_anchor(
    desk, studio, pilates, ada
):
    today = local_today(studio)
    start = add_months(today, -2)
    libre = Plan.objects.create(tenant=studio, name='Libre', price=400000)
    sub = Subscription.objects.create(
        tenant=studio, client=ada, plan=pilates, start_date=start, price_override=200000
    )

    res = api(desk, studio).post(sub_url(sub, 'change-plan'), {'plan': libre.pk}, format='json')

    assert res.status_code == 201
    sub.refresh_from_db()
    # Current period is period 2 (start + 2 months); it runs to the day before
    # period 3 starts, and the new plan opens on that day -- same anchor.
    assert sub.end_date == add_months(start, 3) - timedelta(days=1)
    assert res.data['start_date'] == add_months(start, 3).isoformat()
    assert res.data['plan'] == libre.pk
    # The old row keeps its own price, so the periods it priced are untouched.
    assert sub.price == 200000
    assert res.data['price'] == 400000


def test_changing_plan_before_the_start_just_swaps_it(desk, studio, pilates, ada):
    start = add_months(local_today(studio), 1)
    libre = Plan.objects.create(tenant=studio, name='Libre', price=400000)
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date=start)

    res = api(desk, studio).post(sub_url(sub, 'change-plan'), {'plan': libre.pk}, format='json')

    assert res.status_code == 201
    assert ada.subscriptions.count() == 1
    assert res.data['id'] == sub.pk
    assert res.data['plan'] == libre.pk


def test_changing_to_an_archived_plan_is_rejected(desk, studio, pilates, ada):
    old = Plan.objects.create(tenant=studio, name='Old', price=1, archived=True)
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')

    res = api(desk, studio).post(sub_url(sub, 'change-plan'), {'plan': old.pk}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'plan_archived')
