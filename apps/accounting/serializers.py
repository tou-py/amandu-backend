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
            'appointment', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            # Same mechanism as ServiceSerializer's category: the database will
            # happily link this tenant's entry to another tenant's appointment
            # (TenantOwnedMixin, rule 2), so the field's own queryset is what
            # turns a foreign id into a 400 instead of a cross-tenant link.
            self.fields['appointment'].queryset = Appointment.objects.for_tenant(tenant)


class CashSummarySerializer(serializers.Serializer):
    """
    What a range of days added up to. Declared so the schema says three integers
    instead of promising a bare object, the same reason MovedFollowingSerializer
    exists next door.
    """

    income = serializers.IntegerField(read_only=True)
    expense = serializers.IntegerField(read_only=True)
    balance = serializers.IntegerField(read_only=True)
