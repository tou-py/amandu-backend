from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.accounting.caja_views import CajaSetupView, CajaViewSet
from apps.accounting.views import CashEntryViewSet

app_name = 'accounting'

router = DefaultRouter()
router.register('cash-entries', CashEntryViewSet, basename='cashentry')
router.register('cajas', CajaViewSet, basename='caja')

# Ahead of the router, whose cajas/<pk>/ would otherwise read "setup" as an id.
urlpatterns = [path('cajas/setup/', CajaSetupView.as_view(), name='caja-setup'), *router.urls]
