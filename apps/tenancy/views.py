from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Membership
from apps.tenancy import pricing
from apps.tenancy.permissions import HasActiveMembership, IsTenantOwner
from apps.tenancy.serializers import KyoSubscriptionSerializer, TenantSerializer


class TenantView(RetrieveUpdateAPIView):
    """
    The business the caller is acting for, readable and editable by its owner.

    A singleton with no id in the path, like /api/auth/me/: the tenant is
    already selected by X-Tenant-ID and re-validated against the caller's own
    memberships on every request, so an id here could only ever disagree with
    that -- and every way it could disagree is a bug or an attempt.

    Owner only. An admin runs the diary and the team; the name, the timezone and
    the country are the business itself.
    """

    serializer_class = TenantSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantOwner)

    def get_object(self):
        return self.request.tenant


class KyoSubscriptionView(APIView):
    """
    What this business pays Kyo a month, line by line, and where it stands.

    Owner only, like the rest of the business page. The plan is not stored: it
    is Solo with one professional attending appointments and Equipo with more,
    so it can never disagree with the team (pricing.py).
    """

    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantOwner)

    @extend_schema(responses=KyoSubscriptionSerializer)
    def get(self, request):
        tenant = request.tenant
        names = sorted(m.display_name() for m in Membership.professionals_for(tenant).select_related('user'))
        lines = pricing.monthly_lines(tenant, len(names))
        return Response(KyoSubscriptionSerializer({
            'plan': pricing.plan_name(len(names)),
            'professionals': names,
            'addons': pricing.addons(tenant),
            'lines': lines,
            'total': sum(line['amount'] for line in lines),
            'paid_until': tenant.paid_until,
            'suspends_after': pricing.suspends_after(tenant),
            'billing': pricing.billing_state(tenant, timezone.localdate()),
            'payment_instructions': pricing.payment_instructions(),
            'contact_whatsapp': pricing.contact_whatsapp(),
            'prices': {
                'base': pricing.BASE,
                'per_extra_professional': pricing.PER_EXTRA_PROFESSIONAL,
                'cobros': pricing.COBROS,
                'facturacion': pricing.FACTURACION,
                'facturacion_included': pricing.FACTURACION_INCLUDED,
                'facturacion_extra': pricing.FACTURACION_EXTRA,
            },
        }).data)
