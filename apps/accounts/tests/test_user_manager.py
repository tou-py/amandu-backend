import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_create_user_normalizes_domain_and_hashes_password():
    user = User.objects.create_user(email='Sebastian@EXAMPLE.COM', password='pw')

    # normalize_email lowercases the domain only; the local part is
    # case-sensitive per RFC 5321.
    assert user.email == 'Sebastian@example.com'
    assert user.password != 'pw'
    assert user.check_password('pw')
    assert user.is_active
    assert not user.is_staff


@pytest.mark.django_db
def test_create_user_without_password_is_unusable():
    user = User.objects.create_user(email='invited@example.com')

    assert not user.has_usable_password()


@pytest.mark.django_db
def test_create_user_rejects_empty_email():
    with pytest.raises(ValueError):
        User.objects.create_user(email='', password='pw')


@pytest.mark.django_db
def test_create_superuser_sets_staff_and_superuser():
    user = User.objects.create_superuser(email='root@example.com', password='pw')

    assert user.is_staff
    assert user.is_superuser


@pytest.mark.django_db
def test_create_superuser_rejects_explicit_false_flags():
    with pytest.raises(ValueError):
        User.objects.create_superuser(
            email='a@example.com', password='pw', is_staff=False
        )
    with pytest.raises(ValueError):
        User.objects.create_superuser(
            email='b@example.com', password='pw', is_superuser=False
        )
