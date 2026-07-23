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


class AppointmentSerializer(serializers.ModelSerializer):
    """
    Every relation is scoped to the request tenant, so the four tenant paths
    (own, professional, clients, service) always agree -- the database does not
    check that `end` is derived from the service duration, never sent.
    """

    clients = serializers.PrimaryKeyRelatedField(
        many=True, allow_empty=False, queryset=Client.objects.none()
    )

    class Meta:
        model = Appointment
        fields = (
            'id', 'professional', 'clients', 'service', 'start', 'end', 'status',
            'cancelled_at', 'cancellation_reason', 'notes', 'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'end', 'status', 'cancelled_at', 'cancellation_reason',
            'created_at', 'updated_at',
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            # Only active, bookable memberships of this tenant may be the professional.
            self.fields['professional'].queryset = Membership.objects.filter(
                tenant=tenant,
                status=Membership.Status.ACTIVE,
                attends_appointments=True,
            )
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
