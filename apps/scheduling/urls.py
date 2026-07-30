from rest_framework.routers import DefaultRouter

from apps.scheduling.views import (
    AppointmentTemplateViewSet,
    AppointmentViewSet,
    CategoryViewSet,
    ClientViewSet,
    ProfessionalViewSet,
    ServiceViewSet,
)

app_name = 'scheduling'

router = DefaultRouter()
router.register('clients', ClientViewSet, basename='client')
router.register('categories', CategoryViewSet, basename='category')
router.register('services', ServiceViewSet, basename='service')
router.register('appointments', AppointmentViewSet, basename='appointment')
router.register('appointment-templates', AppointmentTemplateViewSet, basename='appointment-template')
router.register('professionals', ProfessionalViewSet, basename='professional')

urlpatterns = router.urls
