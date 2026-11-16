import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Client, ClientField
from apps.tenancy.models import Tenant

LIST_URL = reverse('scheduling:clientfield-list')
CLIENT_URL = reverse('scheduling:client-list')


def detail_url(field):
    return reverse('scheduling:clientfield-detail', args=[field.pk])


def client_detail_url(client):
    return reverse('scheduling:client-detail', args=[client.pk])


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic', country='AR')


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def owner(db, django_user_model, clinic):
    user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    Membership.objects.create(user=user, tenant=clinic, role=Membership.Role.OWNER)
    return user


@pytest.fixture
def receptionist(db, django_user_model, clinic):
    """Active membership, plain staff role: runs the diary, does not redesign the form."""
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=clinic)
    return user


@pytest.fixture
def allergies(clinic):
    return ClientField.objects.create(
        tenant=clinic, key='allergies', label='Allergies', kind=ClientField.Kind.TEXT
    )


# --- defining the fields -----------------------------------------------------

def test_admin_defines_a_field(owner, clinic):
    res = api(owner, clinic).post(
        LIST_URL, {'key': 'blood-type', 'label': 'Blood type'}, format='json'
    )

    assert res.status_code == 201
    assert ClientField.objects.get(key='blood-type').tenant == clinic


def test_staff_may_read_the_fields_but_not_define_them(receptionist, clinic, allergies):
    """The form cannot be drawn without reading them; reshaping it is not staff's call."""
    http = api(receptionist, clinic)

    assert http.get(LIST_URL).status_code == 200
    assert http.post(LIST_URL, {'key': 'x', 'label': 'X'}, format='json').status_code == 403


def test_a_choice_field_needs_options(owner, clinic):
    res = api(owner, clinic).post(
        LIST_URL, {'key': 'skin', 'label': 'Skin type', 'kind': 'select'}, format='json'
    )

    assert res.status_code == 400
    assert 'options' in res.data


def test_only_a_choice_field_takes_options(owner, clinic):
    res = api(owner, clinic).post(
        LIST_URL,
        {'key': 'weight', 'label': 'Weight', 'kind': 'number', 'options': ['a']},
        format='json',
    )

    assert res.status_code == 400
    assert 'options' in res.data


def test_key_is_unique_per_tenant_but_free_across_tenants(owner, clinic, salon, allergies):
    duplicate = api(owner, clinic).post(
        LIST_URL, {'key': 'allergies', 'label': 'Allergy notes'}, format='json'
    )
    assert duplicate.status_code == 400

    ClientField.objects.create(tenant=salon, key='allergies', label='Allergies')
    assert ClientField.objects.filter(key='allergies').count() == 2


def test_key_cannot_be_changed_once_answers_are_filed_under_it(owner, clinic, allergies):
    res = api(owner, clinic).patch(
        detail_url(allergies), {'key': 'renamed', 'label': 'Allergy notes'}, format='json'
    )

    assert res.status_code == 200
    allergies.refresh_from_db()
    assert allergies.key == 'allergies'
    assert allergies.label == 'Allergy notes'


# --- answering them ----------------------------------------------------------

def test_client_stores_answers_to_the_tenants_own_fields(owner, clinic, allergies):
    res = api(owner, clinic).post(
        CLIENT_URL,
        {'name': 'Ada', 'custom_data': {'allergies': 'penicillin'}},
        format='json',
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').custom_data == {'allergies': 'penicillin'}


def test_answer_to_a_field_this_tenant_never_defined_is_refused(owner, clinic, allergies):
    res = api(owner, clinic).post(
        CLIENT_URL, {'name': 'Ada', 'custom_data': {'colour': 'blonde'}}, format='json'
    )

    assert res.status_code == 400
    assert 'colour' in str(res.data['custom_data'])


def test_another_tenants_field_is_not_a_field_here(owner, clinic, salon):
    ClientField.objects.create(tenant=salon, key='colour', label='Colour formula')

    res = api(owner, clinic).post(
        CLIENT_URL, {'name': 'Ada', 'custom_data': {'colour': 'blonde'}}, format='json'
    )

    assert res.status_code == 400


@pytest.mark.parametrize(
    'kind, options, value, expected',
    [
        ('number', [], 70.5, 70.5),
        ('boolean', [], True, True),
        ('date', [], '2026-03-01', '2026-03-01'),
        ('select', ['dry', 'oily'], 'oily', 'oily'),
    ],
)
def test_each_kind_accepts_its_own_shape(owner, clinic, kind, options, value, expected):
    ClientField.objects.create(
        tenant=clinic, key='answer', label='Answer', kind=kind, options=options
    )

    res = api(owner, clinic).post(
        CLIENT_URL, {'name': 'Ada', 'custom_data': {'answer': value}}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').custom_data['answer'] == expected


@pytest.mark.parametrize(
    'kind, options, value',
    [
        ('number', [], 'seventy'),
        # True is an int in Python; a number field must not file it as 1.
        ('number', [], True),
        ('boolean', [], 'yes'),
        ('date', [], '2026-31-31'),
        ('date', [], 'tomorrow'),
        ('select', ['dry', 'oily'], 'combination'),
        ('text', [], 42),
    ],
)
def test_each_kind_refuses_the_wrong_shape(owner, clinic, kind, options, value):
    ClientField.objects.create(
        tenant=clinic, key='answer', label='Answer', kind=kind, options=options
    )

    res = api(owner, clinic).post(
        CLIENT_URL, {'name': 'Ada', 'custom_data': {'answer': value}}, format='json'
    )

    assert res.status_code == 400
    assert 'answer' in res.data['custom_data']


def test_a_required_field_must_be_answered_on_create(owner, clinic):
    ClientField.objects.create(tenant=clinic, key='consent', label='Consent', required=True)

    res = api(owner, clinic).post(CLIENT_URL, {'name': 'Ada'}, format='json')

    assert res.status_code == 400
    assert 'consent' in res.data['custom_data']


def test_an_unanswered_optional_field_is_not_stored_as_an_empty_answer(owner, clinic, allergies):
    res = api(owner, clinic).post(
        CLIENT_URL, {'name': 'Ada', 'custom_data': {'allergies': ''}}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').custom_data == {}


def test_a_patch_that_ignores_custom_data_leaves_the_answers_alone(owner, clinic, allergies):
    """Adding a required field later must not freeze every edit made for another reason."""
    client = Client.objects.create(
        tenant=clinic, name='Ada', custom_data={'allergies': 'penicillin'}
    )
    ClientField.objects.create(tenant=clinic, key='consent', label='Consent', required=True)

    res = api(owner, clinic).patch(client_detail_url(client), {'name': 'Ada L.'}, format='json')

    assert res.status_code == 200
    client.refresh_from_db()
    assert client.custom_data == {'allergies': 'penicillin'}


def test_custom_data_is_replaced_whole_so_an_answer_can_be_cleared(owner, clinic, allergies):
    client = Client.objects.create(
        tenant=clinic, name='Ada', custom_data={'allergies': 'penicillin'}
    )

    res = api(owner, clinic).patch(
        client_detail_url(client), {'custom_data': {}}, format='json'
    )

    assert res.status_code == 200
    client.refresh_from_db()
    assert client.custom_data == {}


def test_deleting_a_field_takes_its_answers_with_it(owner, clinic, allergies):
    """
    Orphan keys would make every later edit of these clients fail on data the
    operator never typed and cannot see.
    """
    answered = Client.objects.create(
        tenant=clinic, name='Ada', custom_data={'allergies': 'penicillin'}
    )
    untouched = Client.objects.create(tenant=clinic, name='Grace', custom_data={})

    res = api(owner, clinic).delete(detail_url(allergies))

    assert res.status_code == 204
    answered.refresh_from_db()
    assert answered.custom_data == {}

    # And the file is editable again, which is the point of the cleanup.
    assert api(owner, clinic).patch(
        client_detail_url(answered), {'name': 'Ada L.'}, format='json'
    ).status_code == 200
    untouched.refresh_from_db()
    assert untouched.custom_data == {}


def test_custom_data_must_be_an_object(owner, clinic, allergies):
    """A list or a string is not a set of answers, and jsonb would store either."""
    res = api(owner, clinic).post(
        CLIENT_URL, {'name': 'Ada', 'custom_data': ['penicillin']}, format='json'
    )

    assert res.status_code == 400
    assert 'custom_data' in res.data


# --- editing a field without breaking the answers ----------------------------

def test_kind_can_be_corrected_while_nobody_has_answered(owner, clinic, allergies):
    res = api(owner, clinic).patch(detail_url(allergies), {'kind': 'number'}, format='json')

    assert res.status_code == 200
    allergies.refresh_from_db()
    assert allergies.kind == 'number'


def test_kind_cannot_change_once_answers_exist(owner, clinic, allergies):
    """Stored text under a field now declared a number makes that client unsaveable."""
    Client.objects.create(tenant=clinic, name='Ada', custom_data={'allergies': 'penicillin'})

    res = api(owner, clinic).patch(detail_url(allergies), {'kind': 'number'}, format='json')

    assert res.status_code == 400
    allergies.refresh_from_db()
    assert allergies.kind == 'text'


def test_an_option_in_use_cannot_be_removed_but_the_list_can_grow(owner, clinic):
    skin = ClientField.objects.create(
        tenant=clinic, key='skin', label='Skin', kind='select', options=['dry', 'oily']
    )
    Client.objects.create(tenant=clinic, name='Ada', custom_data={'skin': 'dry'})
    http = api(owner, clinic)

    widened = http.patch(
        detail_url(skin), {'options': ['dry', 'oily', 'combination']}, format='json'
    )
    narrowed = http.patch(detail_url(skin), {'options': ['oily']}, format='json')

    assert widened.status_code == 200
    assert narrowed.status_code == 400
    skin.refresh_from_db()
    assert skin.options == ['dry', 'oily', 'combination']


def test_another_tenants_answers_do_not_block_the_edit(owner, clinic, salon, allergies):
    """`for_tenant` on the lookup, or one tenant's data freezes another's form."""
    ClientField.objects.create(tenant=salon, key='allergies', label='Allergies')
    Client.objects.create(tenant=salon, name='Grace', custom_data={'allergies': 'none'})

    res = api(owner, clinic).patch(detail_url(allergies), {'kind': 'number'}, format='json')

    assert res.status_code == 200
