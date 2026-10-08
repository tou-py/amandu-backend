from rest_framework import serializers

from apps.accounting.models import CashEntry
from apps.invoicing.models import Invoice, InvoicingAccount
from apps.invoicing.providers import (
    PROVIDERS,
    InvalidDocument,
    Rejected,
    Unavailable,
    get_provider,
)

CODE = r'^\d{3}$'


class InvoicingAccountSerializer(serializers.ModelSerializer):
    # Write-only, always. A key that issues fiscal documents in the business's
    # name never travels back out; `has_credentials` is all the screen needs.
    api_key = serializers.CharField(write_only=True, required=False, trim_whitespace=True)
    has_credentials = serializers.SerializerMethodField()
    establishment = serializers.RegexField(CODE, required=False)
    point = serializers.RegexField(CODE, required=False)
    next_number = serializers.IntegerField(min_value=1, max_value=9_999_999, required=False)

    class Meta:
        model = InvoicingAccount
        fields = ('provider', 'api_key', 'has_credentials', 'establishment', 'point', 'next_number', 'active')

    def get_has_credentials(self, account) -> bool:
        return bool(account.credentials_encrypted)

    def validate_provider(self, value):
        if value not in PROVIDERS:
            raise serializers.ValidationError(f'Unknown provider. One of: {", ".join(PROVIDERS)}.')
        return value

    def validate(self, attrs):
        api_key = attrs.pop('api_key', None)
        if api_key:
            credentials = {'api_key': api_key}
            provider = attrs.get('provider') or getattr(self.instance, 'provider', '')
            try:
                get_provider(provider).verify(credentials)
            except (Rejected, InvalidDocument) as exc:
                raise serializers.ValidationError({'api_key': str(exc)}, code='invalid_credentials') from exc
            except Unavailable as exc:
                raise serializers.ValidationError(
                    {'api_key': 'The provider is not answering; try again shortly.'}, code='provider_unavailable',
                ) from exc
            attrs['credentials'] = credentials
        return attrs

    def update(self, instance, validated_data):
        # `credentials` is a property, not a field: set it by hand.
        if 'credentials' in validated_data:
            instance.credentials = validated_data.pop('credentials')
        return super().update(instance, validated_data)


class PayerSerializer(serializers.Serializer):
    """Who paid, with what the invoice sheet prefills from their file."""

    id = serializers.UUIDField()
    name = serializers.CharField()
    tax_id = serializers.CharField()
    tax_name = serializers.CharField()


class InvoiceSerializer(serializers.ModelSerializer):
    # As the KuDE prints it: 001-001-0000042.
    display_number = serializers.CharField(source='__str__', read_only=True)
    # What was paid and by whom, so a list of invoices reads without opening any.
    concept = serializers.CharField(source='cash_entry.concept', read_only=True)
    occurred_on = serializers.DateField(source='cash_entry.occurred_on', read_only=True)
    client = PayerSerializer(source='cash_entry.client', read_only=True, allow_null=True)

    class Meta:
        model = Invoice
        fields = (
            'id', 'cash_entry', 'display_number', 'establishment', 'point', 'number', 'total',
            'concept', 'occurred_on', 'client',
            'customer_tax_id', 'customer_name', 'status', 'reference', 'message',
            'approved_at', 'cancelled_at', 'created_at', 'updated_at',
        )
        read_only_fields = fields


class ToIssueSerializer(serializers.ModelSerializer):
    """A payment still waiting for its invoice (none yet, or a rejected one)."""

    client = PayerSerializer(read_only=True, allow_null=True)
    rejected = serializers.SerializerMethodField()

    class Meta:
        model = CashEntry
        fields = ('id', 'amount', 'concept', 'occurred_on', 'payment_method', 'client', 'rejected')
        read_only_fields = fields

    def get_rejected(self, entry) -> str | None:
        """Why the last attempt was refused, when there was one."""
        invoice = getattr(entry, 'invoice', None)
        if invoice is None:
            return None
        return invoice.message or 'Rechazada'


class IssueSerializer(serializers.Serializer):
    cash_entry = serializers.PrimaryKeyRelatedField(queryset=CashEntry.objects.none())
    # Absent: taken from the client file. Present and blank: the anonymous end
    # consumer, even if the file has a RUC.
    tax_id = serializers.RegexField(r'^(\d+(-\d)?)?$', max_length=20, required=False, allow_blank=True)
    tax_name = serializers.CharField(max_length=120, required=False, allow_blank=True)
    save_to_client = serializers.BooleanField(default=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            # Another shop's entry id resolves to nothing, not to its money.
            self.fields['cash_entry'].queryset = CashEntry.objects.for_tenant(tenant)


class PdfQuerySerializer(serializers.Serializer):
    # Not `format`: DRF reads that query parameter as a renderer override.
    layout = serializers.ChoiceField(choices=('a4', 'ticket', 'ticket58'), default='a4')
