import pytest
from django.db import connection, models

from apps.commons.mixins import SoftDeleteMixin, TimestampMixin


class SoftDeleteWidget(TimestampMixin, SoftDeleteMixin):
    """
    Concrete model that exists only for this module: SoftDeleteMixin is
    abstract, so there is nothing to query without one. It also carries
    TimestampMixin because that is the combination real models will use.
    """

    name = models.CharField(max_length=50)

    class Meta:
        app_label = 'commons'


@pytest.fixture(scope='module')
def widget_table(django_db_setup, django_db_blocker):
    """
    The model above has no migration, so its table has to be created by hand.
    Cheaper than shipping a fixtures-only app just to test a mixin.
    """
    with django_db_blocker.unblock():
        with connection.schema_editor() as editor:
            editor.create_model(SoftDeleteWidget)
        yield
        with connection.schema_editor() as editor:
            editor.delete_model(SoftDeleteWidget)


@pytest.mark.django_db
def test_instance_delete_keeps_the_row(widget_table):
    widget = SoftDeleteWidget.objects.create(name='a')

    widget.delete()

    assert widget.deleted_at is not None
    assert not SoftDeleteWidget.objects.filter(pk=widget.pk).exists()
    assert SoftDeleteWidget.all_objects.filter(pk=widget.pk).exists()


@pytest.mark.django_db
def test_queryset_delete_does_not_hard_delete(widget_table):
    """The whole reason SoftDeleteQuerySet exists: qs.delete() skips Model.delete()."""
    SoftDeleteWidget.objects.create(name='a')
    SoftDeleteWidget.objects.create(name='b')

    SoftDeleteWidget.objects.all().delete()

    assert SoftDeleteWidget.objects.count() == 0
    assert SoftDeleteWidget.all_objects.count() == 2


@pytest.mark.django_db
def test_hard_delete_removes_the_row(widget_table):
    widget = SoftDeleteWidget.objects.create(name='a')

    widget.hard_delete()

    assert SoftDeleteWidget.all_objects.count() == 0


@pytest.mark.django_db
def test_queryset_hard_delete_removes_soft_deleted_rows(widget_table):
    SoftDeleteWidget.objects.create(name='a').delete()

    SoftDeleteWidget.all_objects.all().hard_delete()

    assert SoftDeleteWidget.all_objects.count() == 0


@pytest.mark.django_db
def test_restore_requires_the_unfiltered_manager(widget_table):
    widget = SoftDeleteWidget.objects.create(name='a')
    widget.delete()

    # objects has already excluded the row, so this matches nothing.
    assert SoftDeleteWidget.objects.all().restore() == 0

    assert SoftDeleteWidget.all_objects.all().restore() == 1
    assert SoftDeleteWidget.objects.count() == 1


@pytest.mark.django_db
def test_instance_restore(widget_table):
    widget = SoftDeleteWidget.objects.create(name='a')
    widget.delete()

    widget.restore()

    assert widget.deleted_at is None
    assert SoftDeleteWidget.objects.filter(pk=widget.pk).exists()


@pytest.mark.django_db
def test_soft_delete_leaves_updated_at_alone(widget_table):
    """
    delete() saves with update_fields=['deleted_at'], and Django does not add
    auto_now fields to that list. Asserted so the behavior is a decision
    rather than a surprise.
    """
    widget = SoftDeleteWidget.objects.create(name='a')
    before = widget.updated_at

    widget.delete()
    widget.refresh_from_db()

    assert widget.updated_at == before
    assert widget.deleted_at is not None


@pytest.mark.django_db
def test_base_manager_stays_unfiltered(widget_table):
    """
    Forward FK access and cascade deletion both go through _base_manager;
    if it inherited the filter, a soft-deleted parent would raise DoesNotExist.
    """
    widget = SoftDeleteWidget.objects.create(name='a')
    widget.delete()

    assert SoftDeleteWidget._base_manager.filter(pk=widget.pk).exists()
