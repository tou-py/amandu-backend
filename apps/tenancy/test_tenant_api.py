import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

TENANT_URL = reverse('tenancy:tenant')
MEMBERS_URL = reverse('accounts:member-list')


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salon', slug='salon', country='AR', timezone='America/Argentina/Buenos_Aires'
    )


@pytest.fixture
def member(db, django_user_model, salon):
    def make(email, role, **kwargs):
        user = django_user_model.objects.create_user(email=email, password='pw')
        return Membership.objects.create(user=user, tenant=salon, role=role, **kwargs)

    return make


def test_the_owner_reads_their_business(salon, member):
    owner = member('o@example.com', Membership.Role.OWNER)

    res = api(owner.user, salon).get(TENANT_URL)

    assert res.status_code == 200
    assert res.data['name'] == 'Salon'
    assert res.data['country'] == 'AR'
    assert res.data['timezone'] == 'America/Argentina/Buenos_Aires'


def test_the_owner_edits_name_timezone_and_country(salon, member):
    owner = member('o@example.com', Membership.Role.OWNER)

    res = api(owner.user, salon).patch(
        TENANT_URL,
        {'name': 'Salón Lumière', 'timezone': 'America/Santiago', 'country': 'cl'},
        format='json',
    )

    assert res.status_code == 200
    salon.refresh_from_db()
    assert salon.name == 'Salón Lumière'
    assert salon.timezone == 'America/Santiago'
    assert salon.country == 'CL', 'a lowercase code is stored canonically'


def test_the_slug_and_status_cannot_be_edited(salon, member):
    """
    Both would be a different kind of decision: the slug names the tenant
    everywhere, and the status decides whether anyone inside can work at all.
    Read-only fields are ignored by DRF, so the request succeeds and changes
    nothing -- which is the contract being asserted here.
    """
    owner = member('o@example.com', Membership.Role.OWNER)

    res = api(owner.user, salon).patch(
        TENANT_URL, {'slug': 'stolen', 'status': 'closed'}, format='json'
    )

    assert res.status_code == 200
    salon.refresh_from_db()
    assert salon.slug == 'salon'
    assert salon.status == Tenant.Status.ACTIVE


def test_an_unknown_timezone_is_refused(salon, member):
    """Every time in the tenant is rendered through it, so garbage breaks the
    whole agenda at display time rather than here."""
    owner = member('o@example.com', Membership.Role.OWNER)

    res = api(owner.user, salon).patch(TENANT_URL, {'timezone': 'Mars/Olympus'}, format='json')

    assert res.status_code == 400
    assert 'timezone' in res.data


def test_a_blank_name_is_refused(salon, member):
    owner = member('o@example.com', Membership.Role.OWNER)

    res = api(owner.user, salon).patch(TENANT_URL, {'name': '   '}, format='json')

    assert res.status_code == 400


def test_an_admin_may_not_read_or_edit_the_business(salon, member):
    """An admin runs the diary and the team; the business itself is the owner's."""
    admin = member('a@example.com', Membership.Role.ADMIN)
    http = api(admin.user, salon)

    assert http.get(TENANT_URL).status_code == 403
    assert http.patch(TENANT_URL, {'name': 'Mine now'}, format='json').status_code == 403


def test_staff_may_not_reach_the_business_settings(salon, member):
    staff = member('s@example.com', Membership.Role.STAFF)

    assert api(staff.user, salon).get(TENANT_URL).status_code == 403


def test_a_foreign_tenant_id_cannot_select_someone_elses_business(db, salon, member):
    """The header selects among the caller's own memberships; it never grants."""
    other = Tenant.objects.create(name='Other', slug='other')
    owner = member('o@example.com', Membership.Role.OWNER)

    res = api(owner.user, other).get(TENANT_URL)

    assert res.status_code == 403


def test_the_member_list_shows_everyone_not_just_the_bookable(salon, member):
    """
    /api/professionals/ answers "who may be booked" and hides the rest. The team
    screen needs the people, so the receptionist appears here and there does not.
    """
    owner = member('o@example.com', Membership.Role.OWNER, attends_appointments=True)
    member('desk@example.com', Membership.Role.COORDINATOR, attends_appointments=False)
    member('s@example.com', Membership.Role.STAFF, attends_appointments=True)

    res = api(owner.user, salon).get(MEMBERS_URL)

    assert res.status_code == 200
    assert {m['email'] for m in res.data} == {
        'o@example.com', 'desk@example.com', 's@example.com',
    }
    assert [m['role'] for m in res.data] == ['owner', 'coordinator', 'staff'], (
        'ordered by rank, not by insertion'
    )


def test_the_member_list_is_scoped_to_the_active_tenant(db, salon, member, django_user_model):
    clinic = Tenant.objects.create(name='Clinic', slug='clinic')
    owner = member('o@example.com', Membership.Role.OWNER)
    stranger = django_user_model.objects.create_user(email='x@example.com', password='pw')
    Membership.objects.create(user=stranger, tenant=clinic)

    res = api(owner.user, salon).get(MEMBERS_URL)

    assert {m['email'] for m in res.data} == {'o@example.com'}


def test_staff_may_not_list_the_team(salon, member):
    staff = member('s@example.com', Membership.Role.STAFF)

    assert api(staff.user, salon).get(MEMBERS_URL).status_code == 403
