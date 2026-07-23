from rest_framework.routers import DefaultRouter

from apps.scheduling.views import ClientViewSet

app_name = 'scheduling'

router = DefaultRouter()
router.register('clients', ClientViewSet, basename='client')

urlpatterns = router.urls
