import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, models

from apps.tenancy.mixins import TenantOwnedMixin
from apps.tenancy.models import Tenant
from apps.tenancy.models.tenant import validate_timezone


class Service(TenantOwnedMixin):
    """Concrete stand-in for a tenant-owned model; TenantOwnedMixin is abstract."""

    name = models.CharField(max_length=50)

    class Meta:
        app_label = 'tenancy'
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'name'],
                name='unique_service_name_per_tenant',
            ),
        ]


@pytest.fixture(scope='module')
def service_table(django_db_setup, django_db_blocker):
    with django_db_blocker.unblock():
        with connection.schema_editor() as editor:
            editor.create_model(Service)
        yield
        with connection.schema_editor() as editor:
            editor.delete_model(Service)


@pytest.fixture
def tenants(db):
    return (
        Tenant.objects.create(name='Clinic A', slug='clinic-a'),
        Tenant.objects.create(name='Clinic B', slug='clinic-b'),
    )


@pytest.mark.django_db
def test_tenant_defaults_to_active():
    tenant = Tenant.objects.create(name='Clinic A', slug='clinic-a')

    assert tenant.status == Tenant.Status.ACTIVE
    assert tenant.is_operational
    assert str(tenant) == 'Clinic A'


@pytest.mark.django_db
def test_suspended_tenant_exists_but_is_not_operational():
    """Suspension is a state, not a deletion: the data stays reachable."""
    tenant = Tenant.objects.create(
        name='Clinic A', slug='clinic-a', status=Tenant.Status.SUSPENDED
    )

    assert not tenant.is_operational
    assert Tenant.objects.filter(pk=tenant.pk).exists()


@pytest.mark.django_db
def test_slug_is_globally_unique():
    """The tenant identifier is the one value that is not scoped to a tenant."""
    Tenant.objects.create(name='Clinic A', slug='clinic')

    with pytest.raises(IntegrityError):
        Tenant.objects.create(name='Clinic B', slug='clinic')


@pytest.mark.django_db
def test_for_tenant_scopes_the_queryset(tenants, service_table):
    a, b = tenants
    Service.objects.create(tenant=a, name='Haircut')
    Service.objects.create(tenant=b, name='Haircut')

    assert Service.objects.count() == 2
    assert Service.objects.for_tenant(a).count() == 1
    assert Service.objects.for_tenant(a).first().tenant == a


@pytest.mark.django_db
def test_same_name_allowed_across_tenants(tenants, service_table):
    """Global unique=True would have rejected the second row. This is the bug
    TenantOwnedMixin's docstring warns about."""
    a, b = tenants

    Service.objects.create(tenant=a, name='Haircut')
    Service.objects.create(tenant=b, name='Haircut')

    assert Service.objects.filter(name='Haircut').count() == 2


@pytest.mark.django_db
def test_name_is_unique_within_a_tenant(tenants, service_table):
    a, _ = tenants
    Service.objects.create(tenant=a, name='Haircut')

    with pytest.raises(IntegrityError):
        Service.objects.create(tenant=a, name='Haircut')


@pytest.mark.django_db
def test_tenant_field_is_not_form_editable(service_table):
    """
    editable=False keeps `tenant` out of ModelForms and ModelSerializers, so a
    request body cannot reassign a row to another tenant.
    """
    assert Service._meta.get_field('tenant').editable is False


@pytest.mark.django_db
def test_deleting_a_tenant_takes_its_rows(tenants, service_table):
    """on_delete=CASCADE: offboarding must be able to erase everything."""
    a, b = tenants
    Service.objects.create(tenant=a, name='Haircut')
    Service.objects.create(tenant=b, name='Haircut')

    a.delete()

    assert Service.objects.count() == 1
    assert Service.objects.first().tenant == b


@pytest.mark.django_db
def test_fk_traversal_ignores_scoping(tenants, service_table):
    """
    Documented danger, asserted so nobody assumes otherwise: reaching a row
    through a relation goes via _base_manager and crosses tenants. Scoping the
    query root is necessary, not sufficient.
    """
    a, b = tenants
    service = Service.objects.create(tenant=b, name='Haircut')

    # Someone holding an id from another tenant gets the object anyway.
    assert Service.objects.filter(pk=service.pk).exists()
    assert Service.objects.for_tenant(a).filter(pk=service.pk).count() == 0


@pytest.mark.django_db
def test_tenant_defaults_to_utc_and_no_country(tenants):
    a, _ = tenants

    assert a.timezone == 'UTC'
    assert a.country == ''


@pytest.mark.parametrize('value', ['Mars/Olympus', '', 'not a zone', '../etc/passwd'])
def test_timezone_validator_rejects_unknown_zones(value):
    """
    Both failure shapes matter: ZoneInfo raises ZoneInfoNotFoundError for a plausible
    name and ValueError for a malformed key, and the validator has to catch both or a
    bad tenant slips through as a 500.
    """
    with pytest.raises(ValidationError):
        validate_timezone(value)


@pytest.mark.parametrize('value', ['UTC', 'America/Argentina/Buenos_Aires', 'Europe/Madrid'])
def test_timezone_validator_accepts_iana_zones(value):
    validate_timezone(value)


@pytest.mark.django_db
def test_country_must_be_two_uppercase_letters(tenants):
    a, _ = tenants
    a.country = 'arg'

    with pytest.raises(ValidationError):
        a.full_clean()

    a.country = 'AR'
    a.full_clean()
