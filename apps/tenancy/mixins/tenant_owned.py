from django.db import models


class TenantOwnedQuerySet(models.QuerySet):
    """
    Scoping is explicit on purpose: no thread-local, no middleware, no implicit
    "current tenant". The caller must name the tenant it is working for, so a
    query that forgot to scope is visible in review instead of silently
    returning everyone's rows.
    """

    def for_tenant(self, tenant):
        return self.filter(tenant=tenant)


class TenantOwnedMixin(models.Model):
    """
    Abstract base for every model that belongs to a tenant.

    Three things a concrete model MUST get right:

    1. Uniqueness is per tenant, never global. Two tenants may each have a
       service called "Haircut". Always scope the constraint:

           class Meta:
               constraints = [
                   models.UniqueConstraint(
                       fields=['tenant', 'name'],
                       name='unique_service_name_per_tenant',
                   ),
               ]

    2. A ForeignKey to another tenant-owned model does NOT validate that both
       sides share a tenant. The database will happily link a row of tenant A
       to a row of tenant B. Validate it wherever the value comes from outside.

    3. `for_tenant()` filters the root of the query only. A JOIN reaches the
       related table without it, and `obj.related` goes through _base_manager,
       which is unfiltered. Scoping the entry point is necessary, not
       sufficient.

    ponytail: Python-side scoping only. Add Postgres Row-Level Security as the
    backstop once real customer data is in — it is the one layer a forgotten
    filter cannot bypass.
    """

    tenant = models.ForeignKey(
        'tenancy.Tenant',
        on_delete=models.CASCADE,
        # Two apps can each define a model with the same name; without the
        # app_label the reverse accessors collide (fields.E304).
        related_name='%(app_label)s_%(class)s_set',
        # Never settable from a form or a ModelSerializer: assigning the tenant
        # from request data is how one customer writes into another's account.
        # Set it in code, from the authenticated user.
        editable=False,
    )

    objects = TenantOwnedQuerySet.as_manager()

    class Meta:
        abstract = True
