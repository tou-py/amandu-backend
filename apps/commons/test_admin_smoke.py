"""
Every registered admin page loads.

Django's system checks catch a misspelled field name in list_display, but not a
custom form that blows up when rendered, an annotation a display method reads
under the wrong alias, or a select_related path that no longer exists. Those
only surface when a page is actually built -- which, for an admin, means when
somebody is already trying to fix something in production.

Deliberately shallow: it proves the pages render, not what they say. The
behaviour that matters (billing, suspensions) is tested where it lives.
"""
import pytest
from django.contrib import admin
from django.urls import reverse

MODELS = list(admin.site._registry)


@pytest.mark.parametrize('model', MODELS, ids=lambda m: m._meta.label)
def test_changelist_loads(admin_client, model):
    url = reverse(f'admin:{model._meta.app_label}_{model._meta.model_name}_changelist')

    assert admin_client.get(url).status_code == 200


@pytest.mark.parametrize('model', MODELS, ids=lambda m: m._meta.label)
def test_add_page_loads(admin_client, model):
    """A 403 is a pass: some of these refuse creation on purpose (an Appointment
    booked by hand here could span two tenants). What must not happen is a 500."""
    url = reverse(f'admin:{model._meta.app_label}_{model._meta.model_name}_add')

    assert admin_client.get(url).status_code in (200, 403)


def test_a_tenant_owned_change_page_freezes_the_tenant(admin_client, db):
    """The other half of TenantOwnedAdminForm, which the loops above never reach:
    on an existing row the tenant must render disabled. Moving a client between
    tenants would leave its appointments behind, pointing across a boundary the
    rest of the system assumes cannot be crossed."""
    from apps.scheduling.models import Client
    from apps.tenancy.models import Tenant

    tenant = Tenant.objects.create(name='Studio', slug='studio')
    client = Client.objects.create(tenant=tenant, name='Ana')

    response = admin_client.get(
        reverse('admin:scheduling_client_change', args=[client.pk])
    )

    assert response.status_code == 200
    assert response.context['adminform'].form.fields['tenant'].disabled
