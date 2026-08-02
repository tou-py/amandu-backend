import phonenumbers
from django.core.exceptions import ValidationError as DjangoValidationError
from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from apps.accounts.models import Membership
from apps.scheduling.models import (
    Appointment,
    AppointmentClient,
    Category,
    Client,
    ClientField,
    Service,
)


class ClientFieldSerializer(serializers.ModelSerializer):
    """
    The definition of one extra question, not an answer to it.

    `key` is create-only: it is what every stored answer is filed under, so
    changing it would orphan them all silently. Editing the wording is what
    `label` is for; actually renaming the key is a data migration.
    """

    # Declared rather than inferred from the model's JSONField, which has no shape
    # and therefore documents itself as "any JSON at all" -- a generated client
    # gets `unknown` and has to cast its way back to the list this always is.
    # It also does the list-of-strings checking that validate() would otherwise
    # repeat by hand.
    options = serializers.ListField(child=serializers.CharField(), required=False)

    class Meta:
        model = ClientField
        fields = (
            'id', 'key', 'label', 'kind', 'options', 'required', 'position',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance is not None:
            self.fields['key'].read_only = True

    def validate_key(self, value):
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is None:
            return value
        # Mirrors TenantUniqueNameMixin: the constraint guarantees it, this turns
        # the operator's duplicate into a 400 instead of a 500.
        if ClientField.objects.for_tenant(tenant).filter(key=value).exists():
            raise serializers.ValidationError('A field with this key already exists.')
        return value

    def validate(self, attrs):
        """
        A choice field with nothing to choose from is a broken form, and options
        on a text field is a promise nothing keeps. On a PATCH either half may be
        missing, so both fall back to the instance.
        """
        kind = attrs.get('kind') or getattr(self.instance, 'kind', ClientField.Kind.TEXT)
        options = attrs.get('options')
        if options is None:
            options = getattr(self.instance, 'options', [])

        if kind == ClientField.Kind.SELECT:
            if not options:
                raise serializers.ValidationError(
                    {'options': 'A choice field needs at least one option.'}
                )
            if len(set(options)) != len(options):
                raise serializers.ValidationError({'options': 'Options must be distinct.'})
        elif options:
            raise serializers.ValidationError(
                {'options': f'Only a choice field takes options, this one is {kind}.'}
            )

        self._check_answers_survive(kind, options)
        return attrs

    def _check_answers_survive(self, kind, options):
        """
        An edit may not invalidate answers already given.

        Two ways it can. Changing the kind leaves every stored value in the old
        shape -- text answers under a field now declared a number -- and removing
        an option leaves answers pointing at a choice that no longer exists.
        Either way ClientSerializer refuses those clients on their next save, so
        the file becomes uneditable over data nobody can see or fix from the form.
        Same failure the orphaned key would cause after a delete, arriving through
        a different door.

        Only blocked once somebody has actually answered: correcting a field
        minutes after creating it is exactly what an owner should be able to do.
        Widening a choice list is always fine -- it invalidates nothing.
        """
        if self.instance is None:
            return

        removed = set(self.instance.options or []) - set(options or [])
        if kind == self.instance.kind and not removed:
            return

        answered = Client.objects.for_tenant(self.instance.tenant).filter(
            custom_data__has_key=self.instance.key
        )
        if not answered.exists():
            return

        problem = (
            'type' if kind != self.instance.kind
            else f'options ({", ".join(sorted(removed))})'
        )
        raise serializers.ValidationError(
            f'Clients have already answered this field, so its {problem} cannot change. '
            'Create a new field instead.'
        )


class ClientSerializer(serializers.ModelSerializer):
    """
    `tenant` is absent on purpose. It is `editable=False` on TenantOwnedMixin, so
    ModelSerializer never builds a field for it, and no payload can reassign a client
    to another tenant. The view sets it from the authenticated request.
    """

    phone = PhoneNumberField(required=False, allow_blank=True)
    # Same reason as ClientField.options: the model's JSONField documents itself
    # as any JSON whatsoever, so a generated client sees `unknown` where this is
    # always an object keyed by field key. DictField also rejects a list or a
    # string before validate() ever runs.
    custom_data = serializers.DictField(required=False)

    class Meta:
        model = Client
        fields = (
            'id', 'name', 'phone', 'email', 'notes', 'custom_data',
            'created_at', 'updated_at',
        )
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
        self._check_phone(attrs)
        self._check_custom_data(attrs)
        return attrs

    def _check_phone(self, attrs):
        """
        The database constraint still guarantees exact uniqueness; this is the
        wider net in front of it, and it stays check-then-insert, so two
        concurrent creates can both pass here and the constraint decides.
        """
        phone = attrs.get('phone')
        if not phone:
            return

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

    def _check_custom_data(self, attrs):
        """
        Every answer must match a question this tenant actually asks, and answer
        it in the right shape. The column is schemaless jsonb, so this is the only
        thing standing between a form and a file full of garbage.

        `custom_data` is replaced whole, never merged: a form submits the answers
        it has, and merging would make clearing an answer impossible -- the key
        would simply be missing, which is indistinguishable from "not touched".
        So the required check runs on a create and on any write that sends the
        field, and a PATCH that omits it leaves the answers exactly as they were.
        That is also what keeps adding a required field later from freezing every
        client edited for another reason.

        Unknown keys are rejected rather than dropped: silently discarding text
        somebody typed is the one outcome nobody can debug. Orphans left behind
        by a deleted definition cannot reach here -- ClientFieldViewSet strips
        them from the stored data at delete time.
        """
        if 'custom_data' not in attrs and self.instance is not None:
            return

        # Already known to be a dict if present: the DictField above rejects
        # anything else before this runs.
        data = attrs.get('custom_data') or {}
        tenant = self.context['request'].tenant
        definitions = {f.key: f for f in ClientField.objects.for_tenant(tenant)}

        unknown = sorted(set(data) - set(definitions))
        if unknown:
            raise serializers.ValidationError(
                {'custom_data': f'No such field for this tenant: {", ".join(unknown)}.'}
            )

        cleaned = {}
        errors = {}
        for key, definition in definitions.items():
            value = data.get(key)
            # None and '' both mean unanswered. Storing either would be a key
            # that reads as an answer and is not one, so the pair is dropped.
            if value is None or value == '':
                if definition.required:
                    errors[key] = 'This field is required.'
                continue
            try:
                cleaned[key] = definition.clean_value(value)
            except DjangoValidationError as exc:
                errors[key] = exc.messages

        if errors:
            raise serializers.ValidationError({'custom_data': errors})

        attrs['custom_data'] = cleaned


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


class VisitSerializer(serializers.ModelSerializer):
    """
    One appointment as it appears on a client's file: when, for what, with whom,
    whether they turned up and what was written down afterwards.

    Built on the through row and not on Appointment because the file is one
    person's history -- `attendance` is the answer for THIS client, and a slot
    holding four people has four different ones. Reading it from here also means
    the whole timeline is a single query with no per-visit lookup for the roster.
    """

    id = serializers.UUIDField(source='appointment_id', read_only=True)
    start = serializers.DateTimeField(source='appointment.start', read_only=True)
    end = serializers.DateTimeField(source='appointment.end', read_only=True)
    status = serializers.CharField(source='appointment.status', read_only=True)
    service_name = serializers.CharField(source='appointment.service.name', read_only=True)
    professional_name = serializers.CharField(
        source='appointment.professional.display_name', read_only=True
    )
    notes = serializers.CharField(source='appointment.notes', read_only=True)

    class Meta:
        model = AppointmentClient
        fields = (
            'id', 'start', 'end', 'status', 'service_name', 'professional_name',
            'attendance', 'notes',
        )
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
