from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.invoicing.views import InvoiceViewSet, InvoicingAccountView

app_name = 'invoicing'

router = DefaultRouter()
router.register('invoices', InvoiceViewSet, basename='invoice')

urlpatterns = [
    path('invoicing/account/', InvoicingAccountView.as_view(), name='account'),
    *router.urls,
]
