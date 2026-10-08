from django.contrib import admin

from apps.invoicing.models import Invoice, InvoicingAccount
from apps.tenancy.admin import TenantOwnedAdmin


@admin.register(InvoicingAccount)
class InvoicingAccountAdmin(admin.ModelAdmin):
    """The credentials are never shown, only whether there are any."""

    list_display = ('tenant', 'provider', 'establishment', 'point', 'next_number', 'active', 'has_credentials')
    readonly_fields = ('tenant', 'provider', 'next_number')
    list_select_related = ('tenant',)

    @admin.display(boolean=True)
    def has_credentials(self, account):
        return bool(account.credentials_encrypted)

    def has_add_permission(self, request):
        return False


@admin.register(Invoice)
class InvoiceAdmin(TenantOwnedAdmin):
    """Read-only: a fiscal document is changed at the SET, never here."""

    list_display = ('__str__', 'tenant', 'status', 'total', 'customer_name', 'reference', 'created_at')
    list_filter = ('status', 'provider', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('reference', 'customer_name', 'customer_tax_id', 'tenant__name')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
