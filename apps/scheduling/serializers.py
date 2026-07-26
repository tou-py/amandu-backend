from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Category, Client, Service


class ClientSerializer(serializers.ModelSerializer):
    """
    `tenant` is absent on purpose. It is `editable=False` on TenantOwnedMixin, so
    ModelSerializer never builds a field for it, and no payload can reassign a client
    to another tenant. The view sets it from the authenticated request.
    """

    phone = PhoneNumberField(required=False, allow_blank=True)

    class Meta:
        model = Client
        fields = ('id', 'name', 'phone', 'email', 'notes', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The package resolves the region from PHONENUMBER_DEFAULT_REGION, which is a
        # single global value -- but the right region is per tenant, and a model field
        # cannot know which row it is validating. So it is set here, per request.
        # Safe to mutate: DRF deep-copies field instances for every serializer
        # instance precisely so they can be modified individually.
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None and tenant.country:
            self.fields['phone'].region = tenant.country

    def validate(self, attrs):
        """
        The database constraint is what actually guarantees uniqueness; this only
        turns the ordinary case into a 400 with a field error instead of an
        IntegrityError surfacing as a 500. DRF cannot generate the validator itself
        because the constraint spans `tenant`, which is not a serializer field.
        """
        phone = attrs.get('phone')
        if not phone:
            return attrs

        duplicates = Client.objects.for_tenant(
            self.context['request'].tenant
        ).filter(phone=phone)
        if self.instance is not None:
            duplicates = duplicates.exclude(pk=self.instance.pk)

        # check-then-insert, so two concurrent creates can still both pass
        # here and the constraint decides.
        if duplicates.exists():
            raise serializers.ValidationError(
                {'phone': 'A client with this phone already exists.'}
            )
        return attrs


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ('id', 'name', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')


class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = ('id', 'name', 'duration', 'category', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            self.fields['category'].queryset = Category.objects.for_tenant(tenant)


class ProfessionalSerializer(serializers.ModelSerializer):
    """
    Read model of a membership as the agenda needs it: who can be booked and how
    to label them. Deliberately not the membership itself -- role and status are
    access facts, and the agenda only needs an id and a name.
    """

    name = serializers.CharField(source='display_name', read_only=True)

    class Meta:
        model = Membership
        fields = ('id', 'name')


class AppointmentSerializer(serializers.ModelSerializer):
    """
    Every relation is scoped to the request tenant, so the four tenant paths
    (own, professional, clients, service) always agree -- the database does not
    check that `end` is derived from the service duration, never sent.

    The *_name fields exist so an agenda can render a slot without resolving
    three ids per appointment: a calendar showing a week is hundreds of rows, and
    the alternative is hundreds of round trips from a browser. They are read-only
    labels; the ids remain the writable contract.
    """

    clients = serializers.PrimaryKeyRelatedField(
        many=True, allow_empty=False, queryset=Client.objects.none()
    )
    professional_name = serializers.CharField(source='professional.display_name', read_only=True)
    service_name = serializers.CharField(source='service.name', read_only=True)
    client_names = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = (
            'id', 'professional', 'professional_name', 'clients', 'client_names',
            'service', 'service_name', 'start', 'end', 'status',
            'cancelled_at', 'cancellation_reason', 'notes', 'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'end', 'status', 'cancelled_at', 'cancellation_reason',
            'created_at', 'updated_at',
        )

    def get_client_names(self, appointment) -> list[str]:
        return [client.name for client in appointment.clients.all()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            # Only active, bookable memberships of this tenant may be the professional.
            self.fields['professional'].queryset = Membership.professionals_for(tenant)
            self.fields['clients'].child_relation.queryset = Client.objects.for_tenant(tenant)
            self.fields['service'].queryset = Service.objects.for_tenant(tenant)

    def validate(self, attrs):
        tenant = self.context['request'].tenant
        # On a partial update the unchanged sides come from the instance.
        professional = attrs.get('professional') or getattr(self.instance, 'professional', None)
        service = attrs.get('service') or getattr(self.instance, 'service', None)
        start = attrs.get('start') or getattr(self.instance, 'start', None)

        end = start + service.duration
        attrs['end'] = end

        overlapping = (
            Appointment.objects.for_tenant(tenant)
            .filter(professional=professional, start__lt=end, end__gt=start)
            .exclude(status=Appointment.Status.CANCELLED)
        )
        if self.instance is not None:
            overlapping = overlapping.exclude(pk=self.instance.pk)
        if overlapping.exists():
            raise serializers.ValidationError(
                'This professional already has an appointment in that time range.'
            )
        return attrs
