"""
Whether messages to the clients of a public request are filed at all. Nothing
sends them any more: the WAHA sender is being removed, and this goes with it.
"""

from django.conf import settings


def enabled_for(tenant):
    return bool(settings.WAHA_URL and tenant.phone)
