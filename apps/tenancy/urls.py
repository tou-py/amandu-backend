from django.urls import path

from apps.tenancy.views import KyoSubscriptionView, TenantView

app_name = 'tenancy'

urlpatterns = [
    # Singular and id-less: X-Tenant-ID already says which one, and it is
    # re-validated against the caller's memberships on every request.
    path('tenant/', TenantView.as_view(), name='tenant'),
    path('tenant/subscription/', KyoSubscriptionView.as_view(), name='tenant-subscription'),
]
