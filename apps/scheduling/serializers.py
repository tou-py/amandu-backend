import phonenumbers
from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from apps.accounts.models import Membership
from apps.scheduling.models import (
    Appointment,
    AppointmentClient,
    Category,
    Client,
    Service,
)


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
        The database constraint still guarantees exact uniqueness; this is the
        wider net in front of it, and it stays check-then-insert, so two
        concurrent creates can both pass here and the constraint decides.
        """
        phone = attrs.get('phone')
        if not phone:
            return attrs

        others = Client.objects.for_tenant(self.context['request'].tenant)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)

        tail = str(phone.national_number)[-7:]

        for other in others.filter(phone__endswith=tail).iterator():
            match = phonenumbers.is_number_match(str(phone), str(other.phone))
            if match != phonenumbers.MatchType.NO_MATCH:
                raise serializers.ValidationError(
                    {'phone': 'A client with this phone already exists.'}
                )
        return attrs


class TenantUniqueNameMixin:
    """
    Turns `UniqueConstraint(fields=['tenant', 'name'])` into a 400 instead of a 500.

    DRF would normally build a UniqueTogetherValidator from that constraint, but
    it skips any constraint whose sources the serializer does not all map
    (rest_framework/serializers.py, `get_unique_together_validators`) -- and
    `tenant` is never one, because TenantOwnedMixin marks it `editable=False` so
    no payload can reassign a row to another tenant. So the duplicate used to
    reach the database as an unhandled IntegrityError: a 500 reporting an
    operator's ordinary typo as a server fault.

    Deliberately stricter than the constraint, which is case-sensitive: two
    categories called "Hair" and "hair" would be two rows the database accepts
    and no human can tell apart in a select.

    Like ClientSerializer.validate, this stays check-then-insert -- two
    concurrent creates can both pass here and the constraint decides. It is the
    wider, kinder net in front of the guarantee, not the guarantee itself.
    """

    def validate_name(self, value):
        request = self.context.get('request')
        tenant = getattr(request, 'tenant', None)
        if tenant is None:
            return value

        others = self.Meta.model.objects.for_tenant(tenant).filter(name__iexact=value)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)

        if others.exists():
            label = self.Meta.model._meta.verbose_name
            raise serializers.ValidationError(f'A {label} with this name already exists.')
        return value


class CategorySerializer(TenantUniqueNameMixin, serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ('id', 'name', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')


class ServiceSerializer(TenantUniqueNameMixin, serializers.ModelSerializer):
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


class AttendeeSerializer(serializers.ModelSerializer):
    """
    One person in the slot, with whether they turned up. Flattens the through row
    so a client reads `{id, name, attendance}` and never has to know a join table
    sits underneath.
    """

    id = serializers.UUIDField(source='client_id', read_only=True)
    name = serializers.CharField(source='client.name', read_only=True)

    class Meta:
        model = AppointmentClient
        # Read-only here: attendance is recorded through its own action, so it
        # cannot ride along on an edit that was only meant to move the time.
        fields = ('id', 'name', 'attendance')
        read_only_fields = fields


class AttendanceSerializer(serializers.Serializer):
    """
    Body of the attendance action: one person, one verdict. Not a list -- the
    receptionist marks people as they walk in, and a whole-roster payload would
    make every partial update overwrite the ones already recorded.
    """

    client = serializers.UUIDField()
    attendance = serializers.ChoiceField(choices=AppointmentClient.Attendance.choices)


class AppointmentCancelSerializer(serializers.Serializer):
    """
    Body of the cancel action. Not a ModelSerializer on purpose: cancelling takes
    a reason, not an appointment. Declaring it also stops the schema from
    advertising a whole Appointment as the payload, which is what drf-spectacular
    infers for a custom action from the viewset's serializer_class.
    """

    reason = serializers.CharField(required=False, allow_blank=True, trim_whitespace=True)


class AppointmentSerializer(serializers.ModelSerializer):
    """
    Every relation is scoped to the request tenant, so the four tenant paths
    (own, professional, clients, service) always agree -- the database does not
    check that `end` is derived from the service duration, never sent.

    The *_name fields exist so an agenda can render a slot without resolving
    three ids per appointment: a calendar showing a week is hundreds of rows, and
    the alternative is hundreds of round trips from a browser. They are read-only
    labels; the ids remain the writable contract.

    `clients` writes, `attendees` reads. The same people either way: one is the
    list of ids a booking is made from, the other is those people with their
    names and whether they turned up.
    """

    # Write-only: `attendees` already carries these people on the way out, with
    # their names and their attendance. Serialising the bare ids too would send
    # the same roster twice and cost a query per slot to do it.
    clients = serializers.PrimaryKeyRelatedField(
        many=True, allow_empty=False, queryset=Client.objects.none(), write_only=True
    )
    professional_name = serializers.CharField(source='professional.display_name', read_only=True)
    service_name = serializers.CharField(source='service.name', read_only=True)
    attendees = AttendeeSerializer(source='client_links', many=True, read_only=True)

    class Meta:
        model = Appointment
        fields = (
            'id', 'professional', 'professional_name', 'clients', 'attendees',
            'service', 'service_name', 'start', 'end', 'status', 'capacity',
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
            self.fields['professional'].queryset = Membership.professionals_for(tenant)
            self.fields['clients'].child_relation.queryset = Client.objects.for_tenant(tenant)
            self.fields['service'].queryset = Service.objects.for_tenant(tenant)

    def validate_professional(self, professional):
        """
        Whose day this appointment lands in.

        Staff book for themselves; owner, admin and coordinator book for the
        whole team. Enforced here rather than in the view because it is a fact
        about the payload, so it holds for a create and for a PATCH that
        reassigns an existing slot alike.
        """
        membership = self.context['request'].membership
        if professional != membership and not membership.can_schedule_for_others():
            raise serializers.ValidationError(
                'Your role only allows booking appointments for yourself.'
            )
        return professional

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

        self._check_capacity(attrs)
        return attrs

    def _check_capacity(self, attrs):
        """
        The roster may not exceed the slot's ceiling.

        Here and not in a CheckConstraint because the roster lives in
        AppointmentClient: no constraint can count rows in a second table. So
        this is the only enforcement, which is why it has to cover BOTH ways the
        two can cross -- adding people, and lowering the ceiling under the people
        already booked. A PATCH that sends only `capacity` never touches
        `clients`, and one that sends only `clients` never touches `capacity`;
        each side falls back to the instance for the half it did not send.

        ponytail: two writes at once can still cross it -- one lowering capacity
        while the other fills the roster, each reading a value the other is about
        to change. Both are validated in isolation, so the result is an
        over-full slot that refuses the next edit until somebody widens it.
        A select_for_update on the appointment in the update path closes it, the
        day that stops being a curiosity.
        """
        if 'clients' in attrs:
            booked = len(attrs['clients'])
        elif self.instance is not None:
            booked = self.instance.client_links.count()
        else:
            booked = 0

        capacity = attrs.get('capacity')
        if capacity is None:
            # A create that omits the field is not unlimited -- it takes the
            # model default, so the check has to read it from the same place the
            # row is about to. getattr on a None instance would silently skip.
            capacity = getattr(
                self.instance, 'capacity', Appointment._meta.get_field('capacity').default,
            )

        if booked > capacity:
            raise serializers.ValidationError(
                f'This appointment holds {capacity} people and {booked} were booked.'
            )
