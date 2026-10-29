"""
A member's profile photo, from the outside: attaching a key on the team
screen (`PATCH /api/auth/members/<id>/`, whoever may manage that member), on one's
own membership (`PATCH /api/auth/members/me/`), and reading it back wherever
professionals are listed. One fake, the `bucket` fixture; signing runs for real.
"""

import uuid

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

UPLOADS = '/api/uploads/'
ME = reverse('accounts:member-me')
WEBP = 'image/webp'


def api(member):
    http = APIClient()
    http.force_authenticate(user=member.user)
    http.credentials(HTTP_X_TENANT_ID=str(member.tenant_id))
    return http


def detail(member):
    return reverse('accounts:member-detail', args=[member.pk])


def url(key):
    return f'http://localhost:9000/amandu/{key}'


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic', country='AR')


@pytest.fixture
def make(django_user_model):
    def _make(tenant, email, role=Membership.Role.STAFF, attends=False):
        user, _ = django_user_model.objects.get_or_create(email=email)
        return Membership.objects.create(user=user, tenant=tenant, role=role, attends_appointments=attends)

    return _make


@pytest.fixture
def owner(make, salon):
    return make(salon, 'owner@example.com', Membership.Role.OWNER, attends=True)


@pytest.fixture
def stylist(make, salon):
    return make(salon, 'stylist@example.com', attends=True)


def uploaded(signer, target, bucket, size=50_000, content_type=WEBP):
    """Sign for real as `signer`, then put what the browser would have PUT."""
    res = api(signer).post(UPLOADS, {'kind': 'photo', 'target_id': target.pk, 'size': size}, format='json')
    assert res.status_code == 200
    bucket.objects[res.data['key']] = (size, content_type)
    return res.data['key']


def test_the_owner_attaches_a_members_photo_and_it_reads_back_as_a_url(owner, stylist, bucket):
    key = uploaded(owner, stylist, bucket)

    res = api(owner).patch(detail(stylist), {'photo_key': key}, format='json')

    assert res.status_code == 200
    assert res.data['photo_url'] == url(key)
    assert 'photo_key' not in res.data
    listed = {m['id']: m for m in api(owner).get(reverse('accounts:member-list')).data}
    assert listed[stylist.pk]['photo_url'] == url(key)
    assert listed[owner.pk]['photo_url'] is None


def test_an_admin_cannot_set_the_owners_photo(make, salon, owner, bucket):
    admin = make(salon, 'admin@example.com', Membership.Role.ADMIN)
    key = f'{salon.pk}/photo/{uuid.uuid4().hex}.webp'
    bucket.objects[key] = (1000, WEBP)

    res = api(admin).patch(detail(owner), {'photo_key': key}, format='json')

    assert res.status_code == 403
    assert api(owner).get(reverse('accounts:member-list')).data[0]['photo_url'] is None


@pytest.mark.parametrize('key, stored', [
    ('{clinic}/photo/{name}.webp', (1000, WEBP)),       # another tenant's space
    ('{salon}/logo/{name}.webp', (1000, WEBP)),         # another kind
    ('{salon}/photo/{name}.webp', None),        # HEAD finds nothing
    ('{salon}/photo/{name}.webp', (300 * 1024 + 1, WEBP)),
    ('{salon}/photo/{name}.webp', (1000, 'image/svg+xml')),
])
def test_a_key_that_fails_the_attach_check_is_a_400_on_the_field(owner, stylist, salon, clinic, bucket, key, stored):
    key = key.format(salon=salon.pk, clinic=clinic.pk, name=uuid.uuid4().hex)
    if stored:
        bucket.objects[key] = stored

    res = api(owner).patch(detail(stylist), {'photo_key': key}, format='json')

    assert res.status_code == 400
    assert 'photo_key' in res.data


@pytest.fixture
def attached(owner, stylist, bucket, django_capture_on_commit_callbacks):
    key = uploaded(owner, stylist, bucket)
    with django_capture_on_commit_callbacks(execute=True):
        api(owner).patch(detail(stylist), {'photo_key': key}, format='json')
    return key


def test_a_photo_attached_to_one_member_is_refused_for_another(make, salon, owner, bucket, attached):
    other = make(salon, 'other@example.com')

    on_detail = api(owner).patch(detail(other), {'photo_key': attached}, format='json')
    on_me = api(other).patch(ME, {'photo_key': attached}, format='json')

    assert on_detail.status_code == 400 and 'photo_key' in on_detail.data
    assert on_me.status_code == 400 and 'photo_key' in on_me.data


def test_replacing_the_photo_deletes_the_old_object(owner, stylist, bucket, attached, django_capture_on_commit_callbacks):
    new = uploaded(owner, stylist, bucket)

    with django_capture_on_commit_callbacks(execute=True):
        res = api(owner).patch(detail(stylist), {'photo_key': new}, format='json')

    assert res.data['photo_url'] == url(new)
    assert bucket.deleted == [attached]


def test_clearing_the_photo_deletes_the_object(owner, stylist, bucket, attached, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        res = api(owner).patch(detail(stylist), {'photo_key': None}, format='json')

    assert res.data['photo_url'] is None
    assert bucket.deleted == [attached]


def test_a_photo_that_fails_validation_deletes_nothing(
    owner, stylist, salon, bucket, attached, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        res = api(owner).patch(detail(stylist), {'photo_key': f'{salon.pk}/photo/{uuid.uuid4().hex}.webp'}, format='json')

    assert res.status_code == 400
    assert bucket.deleted == []


def test_a_staff_member_sets_their_own_photo_through_members_me(owner, stylist, bucket):
    key = uploaded(stylist, stylist, bucket)

    res = api(stylist).patch(ME, {'photo_key': key}, format='json')

    assert res.status_code == 200
    assert res.data['photo_url'] == url(key)
    listed = {m['id']: m for m in api(owner).get(reverse('accounts:member-list')).data}
    assert listed[stylist.pk]['photo_url'] == url(key)
    assert listed[owner.pk]['photo_url'] is None


def test_members_me_changes_only_the_photo_and_only_in_the_active_business(make, owner, stylist, clinic, bucket):
    elsewhere = make(clinic, 'stylist@example.com', attends=True)
    key = uploaded(stylist, stylist, bucket)

    res = api(stylist).patch(ME, {'photo_key': key, 'role': 'owner', 'status': 'suspended'}, format='json')

    assert res.status_code == 200
    stylist.refresh_from_db()
    elsewhere.refresh_from_db()
    assert (stylist.role, stylist.status) == (Membership.Role.STAFF, Membership.Status.ACTIVE)
    assert elsewhere.photo_key is None


def test_a_staff_member_cannot_set_a_colleagues_photo(make, salon, stylist, bucket):
    colleague = make(salon, 'colleague@example.com', attends=True)
    key = f'{salon.pk}/photo/{uuid.uuid4().hex}.webp'
    bucket.objects[key] = (1000, WEBP)

    assert api(stylist).patch(detail(colleague), {'photo_key': key}, format='json').status_code == 403


def test_members_me_replacing_and_clearing_delete_the_old_object(stylist, bucket, django_capture_on_commit_callbacks):
    first, second = uploaded(stylist, stylist, bucket), uploaded(stylist, stylist, bucket)

    with django_capture_on_commit_callbacks(execute=True):
        api(stylist).patch(ME, {'photo_key': first}, format='json')
        api(stylist).patch(ME, {'photo_key': second}, format='json')
        res = api(stylist).patch(ME, {'photo_key': None}, format='json')

    assert res.data['photo_url'] is None
    assert bucket.deleted == [first, second]


def test_professionals_carry_their_photo_url(owner, stylist, bucket):
    key = uploaded(owner, stylist, bucket)
    api(owner).patch(detail(stylist), {'photo_key': key}, format='json')

    listed = api(stylist).get(reverse('scheduling:professional-list')).data

    assert {p['id']: p['photo_url'] for p in listed} == {owner.pk: None, stylist.pk: url(key)}
