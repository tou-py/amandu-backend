from django.contrib import admin

from apps.scheduling.models import Client


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    """
    Superuser-only reach across every tenant, like the rest of this admin. The
    tenant column is here because without it two identical names from two tenants
    are indistinguishable.
    """

    list_display = ('name', 'tenant', 'phone', 'email', 'created_at')
    list_filter = ('tenant',)
    search_fields = ('name', 'phone', 'email')
    readonly_fields = ('created_at', 'updated_at')
