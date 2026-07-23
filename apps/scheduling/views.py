from apps.scheduling.models import Category, Client, Service
from apps.scheduling.serializers import (
    CategorySerializer,
    ClientSerializer,
    ServiceSerializer,
)
from apps.tenancy.viewsets import TenantScopedModelViewSet


class ClientViewSet(TenantScopedModelViewSet):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer


class CategoryViewSet(TenantScopedModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class ServiceViewSet(TenantScopedModelViewSet):
    queryset = Service.objects.all()
    serializer_class = ServiceSerializer
