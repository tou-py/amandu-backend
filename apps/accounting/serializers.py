from rest_framework import serializers

from apps.accounting.models import CashEntry
from apps.scheduling.billing import refuse_future_day
from apps.scheduling.models import Appointment


class CashEntrySerializer(serializers.ModelSerializer):
    """
    `tenant` is absent on purpose. TenantOwnedMixin marks it `editable=False`, so
    ModelSerializer never builds a field for it and no payload can file an entry
    in another shop's book. The view sets it from the authenticated request.
    """

    # The name the team list shows (Membership.display_name); null when unknown.
    recorded_by_name = serializers.SerializerMethodField()
    # Who paid, for the book; null once the client file is erased.
    client_name = serializers.CharField(source='client.name', read_only=True, default=None, allow_null=True)

    class Meta:
        model = CashEntry
        fields = (
            'id', 'kind', 'amount', 'occurred_on', 'concept', 'payment_method',
            'appointment', 'client', 'client_name', 'subscription', 'period',
            'voided_at', 'voided_on', 'void_reason', 'recorded_by_name', 'created_at', 'updated_at',
        )
        # client/subscription/period are read-only HERE: a payment for a turn or
        # a plan period is filed through the charge actions, which work out the
        # amount and hold the one-payment-per-thing constraints. This endpoint
        # stays the plain till entry.
        read_only_fields = (
            'id', 'client', 'subscription', 'period', 'voided_at', 'voided_on', 'void_reason',
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


    def validate_occurred_on(self, day):
        refuse_future_day(day, self.context['request'].tenant)
        return day

    def get_recorded_by_name(self, entry) -> str | None:
        user = entry.recorded_by
        return user and (user.get_full_name().strip() or user.email)


class MethodTotalSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=CashEntry.PaymentMethod.choices)
    total = serializers.IntegerField()
    count = serializers.IntegerField()


class CashBookSummarySerializer(serializers.Serializer):
    """What came in, for the same filters as the list. Voided entries count for nothing."""

    total = serializers.IntegerField()
    count = serializers.IntegerField()
    by_method = MethodTotalSerializer(many=True)


class VoidSerializer(serializers.Serializer):
    # Required and non-blank: a void with no reason is a reversal nobody can
    # explain later, which is the one thing the record exists to prevent.
    reason = serializers.CharField(trim_whitespace=True)
