from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Invitation, Membership
from apps.scheduling.models import Appointment, Client, Plan, Service, WorkSchedule
from apps.tenancy import pricing
from apps.tenancy.permissions import HasActiveMembership, IsTenantOwner
from apps.tenancy.serializers import (
    AddonsSerializer,
    KyoSubscriptionSerializer,
    SetupFactsSerializer,
    TenantSerializer,
)


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


class SetupFactsView(APIView):
    """What the owner's "Primeros pasos" checklist reads, in one request instead
    of six. Only fetched while the checklist is showing."""

    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantOwner)

    @extend_schema(responses=SetupFactsSerializer)
    def get(self, request):
        tenant = request.tenant

        def owned(model):
            return model.objects.filter(tenant=tenant).exists()

        pending = Invitation.objects.filter(tenant=tenant, status=Invitation.Status.PENDING)
        others = Membership.objects.filter(tenant=tenant).exclude(pk=request.membership.pk)
        return Response(SetupFactsSerializer({
            'logo': bool(tenant.logo_key),
            'photo': bool(request.membership.photo_key),
            'hours': owned(WorkSchedule),
            'services': owned(Service),
            'clients': owned(Client),
            'plans': owned(Plan),
            'invited': pending.exists() or others.exists(),
            'appointments': owned(Appointment),
        }).data)


class KyoSubscriptionView(APIView):
    """
    What this business pays Kyo a month, line by line, and where it stands.

    Owner only, like the rest of the business page. The plan is not stored: it
    is Solo with one professional attending appointments and Equipo with more,
    so it can never disagree with the team (pricing.py).

    PATCH switches the add-ons with immediate effect, billed on the next transfer.
    Facturación stays with Kyo's admin until it ships.
    """

    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantOwner)
    # Dropping an add-on must work in the read-only window: it is how an owner
    # lowers the bill they are late on. Adding one is refused below.
    billing_exempt = True

    @extend_schema(responses=KyoSubscriptionSerializer)
    def get(self, request):
        return Response(self._answer(request.tenant))

    @extend_schema(request=AddonsSerializer, responses=KyoSubscriptionSerializer)
    def patch(self, request):
        serializer = AddonsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = request.tenant
        wanted = serializer.validated_data['addons']
        cobros, reservas = 'cobros' in wanted, 'reservas' in wanted
        adding = (cobros and not tenant.addon_cobros) or (reservas and not tenant.addon_reservas)
        if adding and pricing.billing_state(tenant, timezone.localdate()) == pricing.READ_ONLY:
            raise PermissionDenied('The payment period ended; the account is read-only.', code='billing_read_only')
        tenant.addon_cobros, tenant.addon_reservas = cobros, reservas
        tenant.save(update_fields=['addon_cobros', 'addon_reservas', 'updated_at'])
        return Response(self._answer(tenant))

    def _answer(self, tenant):
        names = sorted(m.display_name() for m in Membership.professionals_for(tenant).select_related('user'))
        lines = pricing.monthly_lines(tenant, len(names))
        return KyoSubscriptionSerializer({
            'plan': pricing.plan_name(len(names)),
            'professionals': names,
            'addons': pricing.addons(tenant),
            'lines': lines,
            'total': sum(line['amount'] for line in lines),
            'paid_until': tenant.paid_until,
            'trial': pricing.on_trial(tenant),
            'trial_until': tenant.trial_until,
            'suspends_after': pricing.suspends_after(tenant),
            'billing': pricing.billing_state(tenant, timezone.localdate()),
            'payment_instructions': pricing.payment_instructions(),
            'contact_whatsapp': pricing.contact_whatsapp(),
            'prices': {
                'base': pricing.BASE,
                'per_extra_professional': pricing.PER_EXTRA_PROFESSIONAL,
                'cobros': pricing.COBROS,
                'reservas': pricing.RESERVAS,
                'facturacion': pricing.FACTURACION,
                'facturacion_included': pricing.FACTURACION_INCLUDED,
                'facturacion_extra': pricing.FACTURACION_EXTRA,
            },
        }).data
