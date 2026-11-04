from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.accounting.cajas import caja_for_income, refuse_before_opening
from apps.accounting.models import Caja, CashEntry
from apps.scheduling.billing import refuse_future_day
from apps.scheduling.models import Appointment


@extend_schema_field(OpenApiTypes.INT)
class OwnCajaField(serializers.PrimaryKeyRelatedField):
    """A live caja of the caller's business: another's id is a 400, not a link."""

    def get_queryset(self):
        return Caja.objects.for_tenant(self.context['request'].tenant).filter(archived=False)


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
    caja = OwnCajaField(required=False, allow_null=True)
    caja_name = serializers.CharField(source='caja.name', read_only=True, default=None, allow_null=True)

    class Meta:
        model = CashEntry
        fields = (
            'id', 'kind', 'amount', 'occurred_on', 'concept', 'payment_method', 'category',
            'caja', 'caja_name', 'appointment', 'client', 'client_name', 'subscription', 'period',
            'voided_at', 'voided_on', 'void_reason', 'recorded_by_name', 'created_at', 'updated_at',
        )
        extra_kwargs = {'amount': {'min_value': 1}, 'payment_method': {'required': True}}
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

    def validate(self, attrs):
        tenant = self.context['request'].tenant
        day = attrs['occurred_on']
        if attrs['kind'] == CashEntry.Kind.INCOME:
            if attrs.get('category'):
                raise serializers.ValidationError({'category': 'Only a salida has a category.'})
            asked = attrs.get('caja')
            attrs['caja'] = caja_for_income(tenant, attrs['payment_method'], day, asked and asked.pk)
            return attrs
        if not tenant.addon_caja:
            raise PermissionDenied('This business does not have Caja.', code='addon_required')
        if not attrs.get('category'):
            raise serializers.ValidationError({'category': 'A salida needs a category.'})
        if not attrs.get('caja'):
            raise serializers.ValidationError({'caja': 'A salida needs a caja.'})
        if attrs.get('appointment'):
            raise serializers.ValidationError({'appointment': 'A salida is not a payment for a turn.'})
        refuse_before_opening(attrs['caja'], day)
        return attrs

    def get_recorded_by_name(self, entry) -> str | None:
        user = entry.recorded_by
        return user and (user.get_full_name().strip() or user.email)


class MethodTotalSerializer(serializers.Serializer):
    payment_method = serializers.ChoiceField(choices=CashEntry.PaymentMethod.choices)
    total = serializers.IntegerField()
    count = serializers.IntegerField()


class CategoryTotalSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=CashEntry.Category.choices)
    total = serializers.IntegerField()
    count = serializers.IntegerField()


class CashBookSummarySerializer(serializers.Serializer):
    """
    What came in, for the same filters as the list. Voided entries count for
    nothing. What went out needs Caja and owner or admin, and is null otherwise.
    """

    total = serializers.IntegerField()
    count = serializers.IntegerField()
    by_method = MethodTotalSerializer(many=True)
    expenses = serializers.IntegerField(allow_null=True)
    by_category = CategoryTotalSerializer(many=True, allow_null=True)
    difference = serializers.IntegerField(allow_null=True, help_text='Entradas minus salidas.')


class VoidSerializer(serializers.Serializer):
    # Required and non-blank: a void with no reason is a reversal nobody can
    # explain later, which is the one thing the record exists to prevent.
    reason = serializers.CharField(trim_whitespace=True)
