"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import AllowAny

from apps.commons.health import healthz
from apps.scheduling.public_pages import booking_page

urlpatterns = [
    path('admin/', admin.site.urls),
    path('healthz/', healthz, name='healthz'),
    # Not under /api/: this one is read by people and indexed by search engines,
    # so it gets a URL a shop can print on a card. Spanish because its readers
    # are, unlike the API.
    path('reservar/<slug:slug>/', booking_page, name='booking-page'),
    path('api/auth/', include('apps.accounts.urls')),
    # The only unauthenticated surface in the product. Its own prefix so that
    # what a stranger can reach is visible here, at the routing table, instead
    # of being a permission_classes line buried in a viewset.
    path('api/public/', include('apps.scheduling.public_urls')),
    path('api/', include('apps.scheduling.urls')),
    path('api/', include('apps.tenancy.urls')),
    # API docs. AllowAny because DEFAULT_PERMISSION_CLASSES is IsAuthenticated and
    # the docs must be reachable without a token. Lock these down before exposing
    # the API publicly if the endpoint list itself is sensitive.
    path('api/schema/', SpectacularAPIView.as_view(permission_classes=[AllowAny]), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema', permission_classes=[AllowAny]), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema', permission_classes=[AllowAny]), name='redoc'),
]
