from django.contrib import admin

from apps.accounting.models import CashEntry
from apps.tenancy.admin import TenantOwnedAdmin


@admin.register(CashEntry)
class CashEntryAdmin(TenantOwnedAdmin):
    """
    Read-only. The book is corrected by voiding, never by editing or deleting:
    a row changed here would rewrite what somebody paid with no trace of who or
    why, and a deleted period payment reopens the period as owed. Void in the app.
    """

    list_display = ('occurred_on', 'tenant', 'kind', 'amount', 'concept', 'client', 'period', 'voided_at')
    list_filter = ('kind', 'payment_method', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('concept', 'client__name', 'tenant__name')
    date_hierarchy = 'occurred_on'
    list_select_related = ('tenant', 'client')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
