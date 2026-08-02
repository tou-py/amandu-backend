from rest_framework.routers import DefaultRouter

from apps.scheduling.views import (
    AppointmentSeriesViewSet,
    AppointmentViewSet,
    CategoryViewSet,
    ClientFieldViewSet,
    ClientViewSet,
    ProfessionalViewSet,
    ServiceViewSet,
)

app_name = 'scheduling'

router = DefaultRouter()
router.register('clients', ClientViewSet, basename='client')
router.register('client-fields', ClientFieldViewSet, basename='clientfield')
router.register('categories', CategoryViewSet, basename='category')
router.register('services', ServiceViewSet, basename='service')
router.register('appointments', AppointmentViewSet, basename='appointment')
router.register('appointment-series', AppointmentSeriesViewSet, basename='appointmentseries')
router.register('professionals', ProfessionalViewSet, basename='professional')

urlpatterns = router.urls
