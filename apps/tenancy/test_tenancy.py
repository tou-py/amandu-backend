import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.scheduling.models import Category
from apps.tenancy.models import Tenant
from apps.tenancy.models.tenant import validate_timezone


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
def test_for_tenant_scopes_the_queryset(tenants):
    a, b = tenants
    Category.objects.create(tenant=a, name='Haircut')
    Category.objects.create(tenant=b, name='Haircut')

    assert Category.objects.count() == 2
    assert Category.objects.for_tenant(a).count() == 1
    assert Category.objects.for_tenant(a).first().tenant == a


@pytest.mark.django_db
def test_same_name_allowed_across_tenants(tenants):
    """Global unique=True would have rejected the second row. This is the bug
    TenantOwnedMixin's docstring warns about."""
    a, b = tenants

    Category.objects.create(tenant=a, name='Haircut')
    Category.objects.create(tenant=b, name='Haircut')

    assert Category.objects.filter(name='Haircut').count() == 2


@pytest.mark.django_db
def test_name_is_unique_within_a_tenant(tenants):
    a, _ = tenants
    Category.objects.create(tenant=a, name='Haircut')

    with pytest.raises(IntegrityError):
        Category.objects.create(tenant=a, name='Haircut')


@pytest.mark.django_db
def test_tenant_field_is_not_form_editable():
    """
    editable=False keeps `tenant` out of ModelForms and ModelSerializers, so a
    request body cannot reassign a row to another tenant.
    """
    assert Category._meta.get_field('tenant').editable is False


@pytest.mark.django_db
def test_deleting_a_tenant_takes_its_rows(tenants):
    """on_delete=CASCADE: offboarding must be able to erase everything."""
    a, b = tenants
    Category.objects.create(tenant=a, name='Haircut')
    Category.objects.create(tenant=b, name='Haircut')

    a.delete()

    assert Category.objects.count() == 1
    assert Category.objects.first().tenant == b


@pytest.mark.django_db
def test_fk_traversal_ignores_scoping(tenants):
    """
    Documented danger, asserted so nobody assumes otherwise: reaching a row
    through a relation goes via _base_manager and crosses tenants. Scoping the
    query root is necessary, not sufficient.
    """
    a, b = tenants
    service = Category.objects.create(tenant=b, name='Haircut')

    # Someone holding an id from another tenant gets the object anyway.
    assert Category.objects.filter(pk=service.pk).exists()
    assert Category.objects.for_tenant(a).filter(pk=service.pk).count() == 0


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
