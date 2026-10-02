from rest_framework import serializers

from apps.accounting.models import CashEntry
from apps.scheduling.models import Appointment


class CashEntrySerializer(serializers.ModelSerializer):
    """
    `tenant` is absent on purpose. TenantOwnedMixin marks it `editable=False`, so
    ModelSerializer never builds a field for it and no payload can file an entry
    in another shop's book. The view sets it from the authenticated request.
    """

    class Meta:
        model = CashEntry
        fields = (
            'id', 'kind', 'amount', 'occurred_on', 'concept', 'payment_method',
            'appointment', 'client', 'subscription', 'period',
            'voided_at', 'void_reason', 'created_at', 'updated_at',
        )
        # client/subscription/period are read-only HERE: a payment for a turn or
        # a plan period is filed through the charge actions, which work out the
        # amount and hold the one-payment-per-thing constraints. This endpoint
        # stays the plain till entry.
        read_only_fields = (
            'id', 'client', 'subscription', 'period', 'voided_at', 'void_reason',
            'created_at', 'updated_at',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            # Same mechanism as ServiceSerializer's category: the database will
            # happily link this tenant's entry to another tenant's appointment
            # (TenantOwnedMixin, rule 2), so the field's own queryset is what
            # turns a foreign id into a 400 instead of a cross-tenant link.
            self.fields['appointment'].queryset = Appointment.objects.for_tenant(tenant)


class VoidSerializer(serializers.Serializer):
    # Required and non-blank: a void with no reason is a reversal nobody can
    # explain later, which is the one thing the record exists to prevent.
    reason = serializers.CharField(trim_whitespace=True)
