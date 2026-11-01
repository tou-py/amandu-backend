"""
The business's place and social presence: what PATCH /api/tenant/ makes of
what an owner pastes, and the links apps/tenancy/presence.py builds from it.
"""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy import presence
from apps.tenancy.models import Tenant

TENANT_URL = reverse('tenancy:tenant')


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY')


@pytest.fixture
def owner(django_user_model, salon):
    user = django_user_model.objects.create_user(email='owner@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(salon.pk))
    return http


@pytest.mark.parametrize('network, pasted, stored', [
    ('instagram', '@salon.lumiere', 'salon.lumiere'),
    ('instagram', 'salon_lumiere', 'salon_lumiere'),
    ('instagram', 'https://www.instagram.com/salonlumiere/?igsh=abc123', 'salonlumiere'),
    ('instagram', 'instagram.com/salonlumiere', 'salonlumiere'),
    ('facebook', 'https://m.facebook.com/salon-lumiere', 'salon-lumiere'),
    ('facebook', 'fb.com/salonlumiere', 'salonlumiere'),
    ('tiktok', 'https://www.tiktok.com/@salonlumiere?lang=es', 'salonlumiere'),
    ('tiktok', '@salonlumiere', 'salonlumiere'),
    ('instagram', '  ', ''),
])
def test_a_handle_or_profile_link_is_stored_as_the_bare_handle(owner, salon, network, pasted, stored):
    res = owner.patch(TENANT_URL, {network: pasted}, format='json')

    assert res.status_code == 200, res.data
    assert res.data[network] == stored
    salon.refresh_from_db()
    assert getattr(salon, network) == stored


@pytest.mark.parametrize('network, pasted', [
    ('instagram', 'https://evil.example/salonlumiere'),
    ('instagram', 'facebook.com/salonlumiere'),
    ('facebook', 'https://instagram.com/salonlumiere'),
    ('tiktok', 'tiktok.com/salonlumiere'),  # a profile is /@handle
    ('instagram', 'instagram.com/'),
    ('instagram', '@salon lumiere'),
    ('tiktok', 'salón'),
])
def test_a_link_elsewhere_or_a_malformed_handle_is_refused(owner, network, pasted):
    res = owner.patch(TENANT_URL, {network: pasted}, format='json')

    assert res.status_code == 400
    assert network in res.data


@pytest.mark.parametrize('url', [
    'https://maps.app.goo.gl/AbC123xyz',
    'https://goo.gl/maps/AbC123',
    'https://www.google.com/maps/place/Salon/@-25.28,-57.63,17z',
    'https://www.google.com.py/maps/place/Salon',
    'https://maps.google.com/?q=Salon',
    'maps.app.goo.gl/AbC123xyz',
])
def test_a_google_maps_link_is_accepted(owner, url):
    res = owner.patch(TENANT_URL, {'maps_url': url}, format='json')

    assert res.status_code == 200, res.data
    assert res.data['maps_url'].startswith('https://')


@pytest.mark.parametrize('url', [
    'https://evil.example/maps',
    'https://www.google.com/search?q=salon',
    'https://goo.gl/AbC123',
    'https://google.com.evil.example/maps',
    'javascript:alert(1)',
])
def test_anything_but_a_google_maps_link_is_refused(owner, url):
    res = owner.patch(TENANT_URL, {'maps_url': url}, format='json')

    assert res.status_code == 400
    assert res.data['maps_url'] == ['Pegá el link de Google Maps de tu negocio.']


@pytest.mark.parametrize('pasted, stored', [
    ('minegocio.com', 'https://minegocio.com'),
    ('http://minegocio.com/turnos', 'http://minegocio.com/turnos'),
    ('', ''),
])
def test_a_website_gets_its_scheme(owner, pasted, stored):
    res = owner.patch(TENANT_URL, {'website': pasted}, format='json')

    assert res.status_code == 200, res.data
    assert res.data['website'] == stored


@pytest.mark.parametrize('pasted', ['ftp://minegocio.com', 'javascript:alert(1)', 'no es una web'])
def test_a_website_that_is_not_http_is_refused(owner, pasted):
    assert owner.patch(TENANT_URL, {'website': pasted}, format='json').status_code == 400


def test_social_links_come_in_order_and_only_when_filled():
    shop = Tenant(instagram='salonlumiere', tiktok='lumiere', website='https://www.minegocio.com/')

    assert presence.social_links(shop) == [
        {'network': 'instagram', 'label': '@salonlumiere', 'url': 'https://www.instagram.com/salonlumiere/'},
        {'network': 'tiktok', 'label': '@lumiere', 'url': 'https://www.tiktok.com/@lumiere'},
        {'network': 'website', 'label': 'minegocio.com', 'url': 'https://www.minegocio.com/'},
    ]
    assert presence.social_links(Tenant(facebook='salon-lumiere')) == [
        {'network': 'facebook', 'label': 'salon-lumiere', 'url': 'https://www.facebook.com/salon-lumiere'},
    ]
    assert presence.social_links(Tenant()) == []


def test_maps_link_prefers_the_owners_pin_then_searches_the_address():
    address = 'Av. Mcal. López 1234 c/ Brasil, Asunción'

    assert presence.maps_link(Tenant(address=address, maps_url='https://maps.app.goo.gl/x')) == (
        'https://maps.app.goo.gl/x'
    )
    assert presence.maps_link(Tenant(address=address)) == (
        'https://www.google.com/maps/search/?api=1&query=Av.+Mcal.+L%C3%B3pez+1234+c%2F+Brasil%2C+Asunci%C3%B3n'
    )
    assert presence.maps_link(Tenant()) == ''


def test_the_embedded_map_needs_both_a_key_and_an_address(settings):
    shop = Tenant(address='Brasil 1234, Asunción')

    settings.GOOGLE_MAPS_EMBED_KEY = ''
    assert presence.maps_embed_url(shop) == ''

    settings.GOOGLE_MAPS_EMBED_KEY = 'k3y'
    assert presence.maps_embed_url(shop) == (
        'https://www.google.com/maps/embed/v1/place?key=k3y&q=Brasil+1234%2C+Asunci%C3%B3n'
    )
    assert presence.maps_embed_url(Tenant(maps_url='https://maps.app.goo.gl/x')) == ''
