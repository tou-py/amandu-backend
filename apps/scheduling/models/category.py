from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class Category(TenantOwnedMixin, TimestampMixin):
    """
    A flat label to group a tenant's services
    """

    name = models.CharField(max_length=80)

    class Meta:
        db_table = 'tb_category'
        ordering = ('name',)
        verbose_name_plural = 'categories'
        constraints = [

            models.UniqueConstraint(
                fields=['tenant', 'name'],
                name='unique_category_name_per_tenant',
            ),
        ]

    def __str__(self):
        return self.name
