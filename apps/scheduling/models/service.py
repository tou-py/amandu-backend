from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class Service(TenantOwnedMixin, TimestampMixin):
    """
    Something a tenant offers and books time for. The
    duration is what turns an appointment's start into its end.
    """

    name = models.CharField(max_length=120)
    # A timedelta. DurationField stores it as a Postgres interval, so end =
    # start + duration is arithmetic the database does, not hand-rolled minutes.
    duration = models.DurationField()
    category = models.ForeignKey(
        'scheduling.Category',
        # Deleting a category must not delete its services -- the service still
        # exists, it just loses its label.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='services',
    )

    class Meta:
        db_table = 'tb_service'
        ordering = ('name',)
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'name'],
                name='unique_service_name_per_tenant',
            ),
        ]

    def __str__(self):
        return self.name
