"""
A service's image, from the outside: signing an upload, attaching the key, and
what the bucket is told to delete. Driven through the HTTP API with one fake,
the `bucket` fixture (storage.head and storage.delete); signing runs for real.
"""

import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Service
from apps.tenancy.models import Tenant

UPLOADS = '/api/uploads/'
WEBP = 'image/webp'


def api(user, tenant):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


def member(django_user_model, tenant, email, role=Membership.Role.STAFF):
    user = django_user_model.objects.create_user(email=email, password='pw')
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return user


def detail(service):
    return reverse('scheduling:service-detail', args=[service.pk])


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic', country='AR')


@pytest.fixture
def owner(django_user_model, salon):
    return member(django_user_model, salon, 'owner@example.com', Membership.Role.OWNER)


@pytest.fixture
def haircut(salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


def test_signing_a_service_upload_returns_a_key_in_the_tenants_space_and_a_signed_put(owner, salon, haircut):
    res = api(owner, salon).post(UPLOADS, {'kind': 'service', 'target_id': haircut.pk, 'size': 50_000}, format='json')

    assert res.status_code == 200
    key = res.data['key']
    assert key.startswith(f'{salon.pk}/service/') and key.endswith('.webp')
    url = urlparse(res.data['upload_url'])
    assert url.path.endswith(key)
    assert 'X-Amz-Signature' in parse_qs(url.query)
    assert res.data['headers'] == {
        'Content-Type': WEBP,
        'Content-Length': '50000',
        'Cache-Control': 'public, max-age=31536000, immutable',
    }


def test_signing_for_another_tenants_service_is_a_404(owner, salon, clinic):
    theirs = Service.objects.create(tenant=clinic, name='Botox', duration=timedelta(minutes=30))

    res = api(owner, salon).post(UPLOADS, {'kind': 'service', 'target_id': theirs.pk, 'size': 1000}, format='json')

    assert res.status_code == 404


def test_staff_cannot_sign_for_a_colleagues_service_but_can_for_their_own(django_user_model, salon, haircut):
    stylist = member(django_user_model, salon, 'stylist@example.com')
    mine = Service.objects.create(
        tenant=salon, name='Color', duration=timedelta(minutes=30), created_by=stylist.memberships.get(),
    )

    other = api(stylist, salon).post(UPLOADS, {'kind': 'service', 'target_id': haircut.pk, 'size': 1000}, format='json')
    own = api(stylist, salon).post(UPLOADS, {'kind': 'service', 'target_id': mine.pk, 'size': 1000}, format='json')

    assert other.status_code == 403
    assert own.status_code == 200


def test_an_admin_cannot_sign_for_the_owners_photo(django_user_model, owner, salon):
    admin = member(django_user_model, salon, 'admin@example.com', Membership.Role.ADMIN)

    res = api(admin, salon).post(
        UPLOADS, {'kind': 'photo', 'target_id': owner.memberships.get().pk, 'size': 1000}, format='json',
    )

    assert res.status_code == 403


def test_only_the_owner_signs_for_the_logo(django_user_model, owner, salon):
    admin = member(django_user_model, salon, 'admin@example.com', Membership.Role.ADMIN)

    assert api(admin, salon).post(UPLOADS, {'kind': 'logo', 'size': 1000}, format='json').status_code == 403
    assert api(owner, salon).post(UPLOADS, {'kind': 'logo', 'size': 1000}, format='json').status_code == 200


@pytest.mark.parametrize('kind, cap', [('logo', 300 * 1024), ('photo', 300 * 1024), ('service', 1024 * 1024)])
def test_a_size_over_the_kinds_cap_is_a_400(owner, salon, haircut, kind, cap):
    target = {'photo': owner.memberships.get().pk, 'service': haircut.pk}.get(kind, '')

    over = api(owner, salon).post(UPLOADS, {'kind': kind, 'target_id': target, 'size': cap + 1}, format='json')
    at = api(owner, salon).post(UPLOADS, {'kind': kind, 'target_id': target, 'size': cap}, format='json')

    assert over.status_code == 400
    assert at.status_code == 200


def test_signing_answers_404_then_403_before_the_size_cap(django_user_model, owner, salon, clinic, haircut):
    theirs = Service.objects.create(tenant=clinic, name='Botox', duration=timedelta(minutes=30))
    stylist = member(django_user_model, salon, 'stylist@example.com')
    huge = 10 * 1024 * 1024

    foreign = api(owner, salon).post(UPLOADS, {'kind': 'service', 'target_id': theirs.pk, 'size': huge}, format='json')
    forbidden = api(stylist, salon).post(UPLOADS, {'kind': 'service', 'target_id': haircut.pk, 'size': huge}, format='json')

    assert foreign.status_code == 404
    assert forbidden.status_code == 403


def test_the_signed_put_pins_type_length_and_cache(owner, salon, haircut):
    res = api(owner, salon).post(UPLOADS, {'kind': 'service', 'target_id': haircut.pk, 'size': 1000}, format='json')

    signed = parse_qs(urlparse(res.data['upload_url']).query)['X-Amz-SignedHeaders'][0].split(';')
    assert {'content-type', 'content-length', 'cache-control'} <= set(signed)


def uploaded(owner, salon, haircut, bucket, size=50_000, content_type=WEBP):
    """Sign for real, then put what the browser would have PUT in the fake bucket."""
    res = api(owner, salon).post(UPLOADS, {'kind': 'service', 'target_id': haircut.pk, 'size': size}, format='json')
    bucket.objects[res.data['key']] = (size, content_type)
    return res.data['key']


def test_an_uploaded_key_attaches_and_reads_back_as_a_public_url(owner, salon, haircut, bucket):
    key = uploaded(owner, salon, haircut, bucket)

    res = api(owner, salon).patch(detail(haircut), {'image_key': key}, format='json')

    assert res.status_code == 200
    assert res.data['image_url'] == f'http://localhost:9000/amandu/{key}'
    assert 'image_key' not in res.data
    listed = api(owner, salon).get(reverse('scheduling:service-list')).data['results']
    assert listed[0]['image_url'] == f'http://localhost:9000/amandu/{key}'


def test_a_service_without_an_image_reads_back_a_null_url(owner, salon, haircut):
    assert api(owner, salon).get(detail(haircut)).data['image_url'] is None


def test_a_key_from_another_tenants_space_is_refused(owner, salon, clinic, haircut, bucket):
    foreign = f'{clinic.pk}/service/{uuid.uuid4().hex}.webp'
    bucket.objects[foreign] = (1000, WEBP)

    res = api(owner, salon).patch(detail(haircut), {'image_key': foreign}, format='json')

    assert res.status_code == 400
    assert 'image_key' in res.data


def test_a_key_of_another_kind_is_refused(owner, salon, haircut, bucket):
    logo = api(owner, salon).post(UPLOADS, {'kind': 'logo', 'size': 1000}, format='json').data['key']
    bucket.objects[logo] = (1000, WEBP)

    res = api(owner, salon).patch(detail(haircut), {'image_key': logo}, format='json')

    assert res.status_code == 400


def test_a_key_nothing_was_uploaded_to_is_refused(owner, salon, haircut, bucket):
    key = api(owner, salon).post(
        UPLOADS, {'kind': 'service', 'target_id': haircut.pk, 'size': 1000}, format='json',
    ).data['key']

    res = api(owner, salon).patch(detail(haircut), {'image_key': key}, format='json')

    assert res.status_code == 400


@pytest.mark.parametrize('size, content_type', [(1024 * 1024 + 1, WEBP), (1000, 'image/svg+xml')])
def test_an_upload_over_the_cap_or_not_webp_is_refused(owner, salon, haircut, bucket, size, content_type):
    key = uploaded(owner, salon, haircut, bucket, size=1000)
    bucket.objects[key] = (size, content_type)

    res = api(owner, salon).patch(detail(haircut), {'image_key': key}, format='json')

    assert res.status_code == 400
    assert api(owner, salon).get(detail(haircut)).data['image_url'] is None


@pytest.fixture
def attached(owner, salon, haircut, bucket, django_capture_on_commit_callbacks):
    key = uploaded(owner, salon, haircut, bucket)
    with django_capture_on_commit_callbacks(execute=True):
        api(owner, salon).patch(detail(haircut), {'image_key': key}, format='json')
    return key


def test_replacing_the_image_deletes_the_old_object(
    owner, salon, haircut, bucket, attached, django_capture_on_commit_callbacks,
):
    new = uploaded(owner, salon, haircut, bucket)

    with django_capture_on_commit_callbacks(execute=True):
        res = api(owner, salon).patch(detail(haircut), {'image_key': new}, format='json')

    assert res.status_code == 200
    assert bucket.deleted == [attached]


def test_clearing_the_image_deletes_the_object(
    owner, salon, haircut, bucket, attached, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        res = api(owner, salon).patch(detail(haircut), {'image_key': None}, format='json')

    assert res.status_code == 200
    assert res.data['image_url'] is None
    assert bucket.deleted == [attached]


def test_an_edit_that_leaves_the_image_alone_deletes_nothing(
    owner, salon, haircut, bucket, attached, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        api(owner, salon).patch(detail(haircut), {'name': 'Corte'}, format='json')
        api(owner, salon).patch(detail(haircut), {'image_key': attached}, format='json')

    assert bucket.deleted == []


def test_deleting_the_service_deletes_its_image(
    owner, salon, haircut, bucket, attached, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        res = api(owner, salon).delete(detail(haircut))

    assert res.status_code == 204
    assert bucket.deleted == [attached]


def test_a_request_that_fails_validation_deletes_nothing(
    owner, salon, haircut, bucket, attached, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        res = api(owner, salon).patch(detail(haircut), {'image_key': f'{salon.pk}/service/{uuid.uuid4().hex}.webp'}, format='json')

    assert res.status_code == 400
    assert bucket.deleted == []
