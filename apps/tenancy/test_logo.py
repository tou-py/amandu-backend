"""
The business's logo, from the outside: attaching an uploaded key through
PATCH /api/tenant/ and what the bucket is told to delete. Same single fake as
the service images, the `bucket` fixture; signing runs for real.
"""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

UPLOADS = '/api/uploads/'
TENANT_URL = reverse('tenancy:tenant')
WEBP = 'image/webp'


def api(user, tenant):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def owner(django_user_model, salon):
    user = django_user_model.objects.create_user(email='owner@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)
    return api(user, salon)


def uploaded(owner, bucket, size=50_000, content_type=WEBP):
    """Sign for real, then put what the browser would have PUT in the fake bucket."""
    key = owner.post(UPLOADS, {'kind': 'logo', 'size': size}, format='json').data['key']
    bucket.objects[key] = (size, content_type)
    return key


def test_an_uploaded_logo_attaches_and_reads_back_as_a_public_url(owner, bucket):
    key = uploaded(owner, bucket)

    res = owner.patch(TENANT_URL, {'logo_key': key}, format='json')

    assert res.status_code == 200
    assert res.data['logo_url'] == f'http://localhost:9000/amandu/{key}'
    assert 'logo_key' not in res.data
    assert owner.get(TENANT_URL).data['logo_url'] == f'http://localhost:9000/amandu/{key}'


def test_a_business_without_a_logo_reads_back_a_null_url(owner):
    assert owner.get(TENANT_URL).data['logo_url'] is None


def test_a_logo_from_another_tenants_space_is_refused(owner, bucket):
    clinic = Tenant.objects.create(name='Clinic', slug='clinic', country='AR')
    foreign = f'{clinic.pk}/logo/stolen.webp'
    bucket.objects[foreign] = (1000, WEBP)

    res = owner.patch(TENANT_URL, {'logo_key': foreign}, format='json')

    assert res.status_code == 400
    assert 'logo_key' in res.data


def test_a_key_of_another_kind_is_refused(owner, salon, bucket):
    photo = f'{salon.pk}/photo/face.webp'
    bucket.objects[photo] = (1000, WEBP)

    assert owner.patch(TENANT_URL, {'logo_key': photo}, format='json').status_code == 400


def test_a_key_nothing_was_uploaded_to_is_refused(owner, bucket):
    key = owner.post(UPLOADS, {'kind': 'logo', 'size': 1000}, format='json').data['key']

    assert owner.patch(TENANT_URL, {'logo_key': key}, format='json').status_code == 400


@pytest.mark.parametrize('size, content_type', [(300 * 1024 + 1, WEBP), (1000, 'image/svg+xml')])
def test_an_upload_over_the_cap_or_not_webp_is_refused(owner, bucket, size, content_type):
    key = uploaded(owner, bucket, size=1000)
    bucket.objects[key] = (size, content_type)

    res = owner.patch(TENANT_URL, {'logo_key': key}, format='json')

    assert res.status_code == 400
    assert owner.get(TENANT_URL).data['logo_url'] is None


def test_an_admin_cannot_attach_a_logo(django_user_model, salon, owner, bucket):
    user = django_user_model.objects.create_user(email='admin@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.ADMIN)
    key = uploaded(owner, bucket)

    res = api(user, salon).patch(TENANT_URL, {'logo_key': key}, format='json')

    assert res.status_code == 403
    assert owner.get(TENANT_URL).data['logo_url'] is None


@pytest.fixture
def attached(owner, bucket, django_capture_on_commit_callbacks):
    key = uploaded(owner, bucket)
    with django_capture_on_commit_callbacks(execute=True):
        owner.patch(TENANT_URL, {'logo_key': key}, format='json')
    return key


def test_replacing_the_logo_deletes_the_old_object(owner, bucket, attached, django_capture_on_commit_callbacks):
    new = uploaded(owner, bucket)

    with django_capture_on_commit_callbacks(execute=True):
        res = owner.patch(TENANT_URL, {'logo_key': new}, format='json')

    assert res.status_code == 200
    assert bucket.deleted == [attached]


def test_removing_the_logo_deletes_the_object(owner, bucket, attached, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        res = owner.patch(TENANT_URL, {'logo_key': None}, format='json')

    assert res.status_code == 200
    assert res.data['logo_url'] is None
    assert bucket.deleted == [attached]


def test_an_edit_that_leaves_the_logo_alone_deletes_nothing(
    owner, bucket, attached, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        owner.patch(TENANT_URL, {'name': 'Salón Lumière'}, format='json')
        owner.patch(TENANT_URL, {'logo_key': attached}, format='json')

    assert bucket.deleted == []


def test_a_request_that_fails_validation_deletes_nothing(
    owner, salon, bucket, attached, django_capture_on_commit_callbacks,
):
    with django_capture_on_commit_callbacks(execute=True):
        res = owner.patch(TENANT_URL, {'logo_key': f'{salon.pk}/logo/missing.webp'}, format='json')

    assert res.status_code == 400
    assert bucket.deleted == []
    assert owner.get(TENANT_URL).data['logo_url'].endswith(attached)
