"""
The unauthenticated routes, kept in a URLconf of their own so that what is
public is a list somebody can read, not a property to be discovered by walking
the router.
"""

from django.urls import path

from apps.scheduling import public

app_name = 'public'

urlpatterns = [
    path('<slug:slug>/', public.shop_detail, name='shop-detail'),
    path('<slug:slug>/availability/', public.availability, name='availability'),
    path('<slug:slug>/bookings/', public.book, name='book'),
]
