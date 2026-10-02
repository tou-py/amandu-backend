from django.urls import path
from rest_framework.routers import DefaultRouter

from apps.scheduling.views import (
    AppointmentSeriesViewSet,
    AppointmentViewSet,
    CategoryViewSet,
    ClientFieldViewSet,
    ClientViewSet,
    PlanViewSet,
    ProfessionalViewSet,
    ReceivablesView,
    ServiceViewSet,
    SubscriptionViewSet,
    TimeOffViewSet,
    WorkScheduleViewSet,
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
router.register('work-schedules', WorkScheduleViewSet, basename='workschedule')
router.register('time-off', TimeOffViewSet, basename='timeoff')
router.register('plans', PlanViewSet, basename='plan')
router.register('subscriptions', SubscriptionViewSet, basename='subscription')

urlpatterns = [
    path('receivables/', ReceivablesView.as_view(), name='receivables'),
    *router.urls,
]
