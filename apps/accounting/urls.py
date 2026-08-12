from rest_framework.routers import DefaultRouter

from apps.accounting.views import CashEntryViewSet

app_name = 'accounting'

router = DefaultRouter()
router.register('cash-entries', CashEntryViewSet, basename='cashentry')

urlpatterns = router.urls
