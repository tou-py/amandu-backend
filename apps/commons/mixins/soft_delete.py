from django.db import models
from django.utils import timezone


class SoftDeleteQuerySet(models.QuerySet):
    """
    QuerySet, whose delete() marks rows instead of removing them.

    Overriding at this level is mandatory, not stylistic: QuerySet.delete()
    builds a Collector and emits SQL itself (django/db/models/query.py), so it
    never reaches Model.delete(). Without this class,
    `Model.objects.filter(...).delete()` would hard delete in silence.
    """

    def delete(self):
        """
        Returns the row count from UPDATE, not QuerySet.delete()'s
        (total, {label: count}) tuple. Anything unpacking that tuple breaks.
        """
        return self.update(deleted_at=timezone.now())

    def hard_delete(self):
        """Really remove the rows, cascading as Django normally would."""
        return super().delete()

    def restore(self):
        """
        Reachable only through a manager that does not filter, i.e.
        `Model.all_objects` — the default manager already excluded these rows,
        so calling it on `Model.objects` matches nothing.
        """
        return self.update(deleted_at=None)


class SoftDeleteManager(models.Manager.from_queryset(SoftDeleteQuerySet)):
    """Default manager: hides soft-deleted rows."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SoftDeleteMixin(models.Model):
    """
    Abstract base for models that must keep historical rows instead of
    removing them.

    `Model.objects` hides deleted rows; `Model.all_objects` sees everything.
    Django's own `_base_manager` stays unfiltered (it defaults to a plain
    Manager, see django/db/models/options.py), which is why a soft-deleted
    parent is still reachable through a forward FK and why a real cascade
    still finds every row.

    Two consequences to handle in the concrete model:

    1. `unique=True` breaks. validate_unique() goes through the default
       manager and cannot see deleted rows, so a form validates, and the
       database then raises IntegrityError. Use a partial constraint instead:

           class Meta:
               constraints = [
                   models.UniqueConstraint(
                       fields=['slug'],
                       condition=models.Q(deleted_at__isnull=True),
                       name='unique_active_slug',
                   ),
               ]

    2. Soft delete does not cascade. No DELETE is issued, so Django never
       collects related objects, and children outlive their parent. Cascade
       explicitly when the domain needs it.

    pre_delete/post_delete do not fire either, for the same reason.
    """

    # editable=False keeps it out of ModelForms and the admin: delete() and
    # restore() own this field.
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)

    # Declaration order is load-bearing: the first manager becomes
    # _default_manager, which is what the admin, reverse relations
    # (order.items) and validate_unique() all use.
    objects = SoftDeleteManager()
    all_objects = SoftDeleteQuerySet.as_manager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        # update_fields writes one column, so a concurrent edit to any other
        # field survives. The tradeoff: an auto_now field left out of the list
        # is not refreshed, so combining this with TimestampMixin leaves
        # updated_at on the last content change. deleted_at is the timestamp
        # of this one.
        self.deleted_at = timezone.now()
        self.save(using=using, update_fields=['deleted_at'])

    def hard_delete(self, using=None, keep_parents=False):
        return super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        self.deleted_at = None
        self.save(update_fields=['deleted_at'])
