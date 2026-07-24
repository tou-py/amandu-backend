import pytest
from django.core.management import call_command

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Client, Service
from apps.tenancy.models import Tenant


@pytest.mark.django_db
def test_seed_demo_is_idempotent(settings):
    """Running the seeder twice must leave the same rows, not double them."""
    settings.DEBUG = True  # test settings ship DEBUG=False; the seeder is dev-only
    call_command('seed_demo')
    counts = {
        model: model.objects.count()
        for model in (Tenant, Membership, Service, Client, Appointment)
    }

    call_command('seed_demo')

    for model, first in counts.items():
        assert model.objects.count() == first, f'{model.__name__} duplicated on re-run'


@pytest.mark.django_db
def test_seed_demo_refuses_in_prod(settings):
    settings.DEBUG = False
    with pytest.raises(Exception):
        call_command('seed_demo')
    assert not Tenant.objects.exists()
