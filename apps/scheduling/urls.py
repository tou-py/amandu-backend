from rest_framework.routers import DefaultRouter

from apps.scheduling.views import (
    AppointmentViewSet,
    CategoryViewSet,
    ClientViewSet,
    ServiceViewSet,
)

app_name = 'scheduling'

router = DefaultRouter()
router.register('clients', ClientViewSet, basename='client')
router.register('categories', CategoryViewSet, basename='category')
router.register('services', ServiceViewSet, basename='service')
router.register('appointments', AppointmentViewSet, basename='appointment')

urlpatterns = router.urls
