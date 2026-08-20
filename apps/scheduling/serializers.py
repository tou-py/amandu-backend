from datetime import datetime
from zoneinfo import ZoneInfo

import phonenumbers
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from drf_spectacular.utils import extend_schema_field
from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from apps.accounts.models import Membership
from apps.scheduling.models import (
    MAX_OCCURRENCES,
    Appointment,
    AppointmentClient,
    AppointmentSeries,
    Category,
    Client,
    ClientField,
    Service,
    TimeOff,
    WorkSchedule,
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
    # Derived, never stored: Client.plan_state() answers it from `paid_until` and
    # today's date. Sent alongside the two raw fields rather than instead of them
    # because the form edits the plan and the file reads whether it covers today,
    # and those are different questions about the same two columns.
    plan_state = serializers.ChoiceField(choices=Client.PLAN_STATES, read_only=True)

    class Meta:
        model = Client
        fields = (
            'id', 'name', 'phone', 'email', 'notes', 'custom_data',
            'monthly_fee', 'paid_until', 'plan_state',
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
        fields = ('id', 'name', 'price', 'duration', 'category', 'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            self.fields['category'].queryset = Category.objects.for_tenant(tenant)


class ProfessionalScopedMixin:
    """
    Scopes the `professional` field to this tenant's bookable staff.

    Without it the field accepts any membership id in the database, which is how
    one tenant writes a working week into another's diary. The queryset is the
    same `professionals_for` the agenda validates against, so "who can be given
    hours" and "who can be booked" cannot drift apart.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            self.fields['professional'].queryset = Membership.professionals_for(tenant)


class WorkScheduleSerializer(ProfessionalScopedMixin, serializers.ModelSerializer):
    class Meta:
        model = WorkSchedule
        fields = (
            'id', 'professional', 'weekday', 'start_time', 'end_time',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        """
        Checked here as well as by the database constraint, because a 400 naming
        the field is a usable answer and a 500 from an IntegrityError is not.
        """
        start = attrs.get('start_time', getattr(self.instance, 'start_time', None))
        end = attrs.get('end_time', getattr(self.instance, 'end_time', None))
        if start is not None and end is not None and end <= start:
            raise serializers.ValidationError(
                {'end_time': 'The end of a shift must come after its start.'}
            )
        return attrs


class TimeOffSerializer(ProfessionalScopedMixin, serializers.ModelSerializer):
    """
    A stretch taken out of the working week. `professional` null on purpose
    means the whole tenant is shut, so it stays writable rather than being
    filled in from the request.
    """

    class Meta:
        model = TimeOff
        fields = ('id', 'professional', 'start', 'end', 'reason',
                  'created_at', 'updated_at')
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate(self, attrs):
        start = attrs.get('start', getattr(self.instance, 'start', None))
        end = attrs.get('end', getattr(self.instance, 'end', None))
        if start is not None and end is not None and end <= start:
            raise serializers.ValidationError(
                {'end': 'Time off must end after it starts.'}
            )
        return attrs


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
    One person in the slot, with whether they turned up and whether a monthly
    plan covers them. Flattens the through row so a client reads
    `{id, name, attendance, plan_state}` and never has to know a join table sits
    underneath.
    """

    id = serializers.UUIDField(source='client_id', read_only=True)
    name = serializers.CharField(source='client.name', read_only=True)
    # Per attendee and not per appointment: a group class holds four people and
    # each one is covered or not on their own. The charge step reads this to
    # decide whether to ask for money from this person at all, so it has to ride
    # on the roster the agenda already has -- fetching the client file per name
    # would be a round trip per person, per slot, per day on screen.
    #
    # Free of extra queries only because AppointmentViewSet prefetches
    # `client_links__client`; reading it off `client.name`'s own object is what
    # keeps it that way.
    plan_state = serializers.ChoiceField(
        choices=Client.PLAN_STATES, source='client.plan_state', read_only=True
    )

    class Meta:
        model = AppointmentClient
        # Read-only here: attendance is recorded through its own action, so it
        # cannot ride along on an edit that was only meant to move the time.
        fields = ('id', 'name', 'attendance', 'plan_state')
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


class AppointmentTemplateMixin:
    """
    The rules that describe a booking, wherever it comes from: one made by hand
    and one generated fifty at a time by a series answer to the same three
    questions -- may this tenant use these ids, may this caller book that
    professional, and does the roster fit.

    Shared rather than repeated because a rule written twice is a rule that
    disagrees with itself eventually. Declared fields stay on each serializer:
    DRF collects those from the class and from serializer bases, not from a
    plain mixin, so putting them here would silently drop them.
    """

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
        about the payload, so it holds for a create, for a PATCH that reassigns
        an existing slot, and for a whole series alike.
        """
        membership = self.context['request'].membership
        if professional != membership and not membership.can_schedule_for_others():
            raise serializers.ValidationError(
                'Your role only allows booking appointments for yourself.'
            )
        return professional

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


class AppointmentSerializer(AppointmentTemplateMixin, serializers.ModelSerializer):
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
    # Null when the service is not charged per session, which is the only signal
    # for that (Service.price). Carried on the slot because the charge step lives
    # on the appointment sheet and the agenda never fetches the service
    # catalogue: without it here the front end cannot tell a free class from an
    # unpriced one, and somebody ends up re-adding the business-type flag this
    # design exists to avoid. Free of queries -- `service` is already
    # select_related for `service_name`.
    service_price = serializers.IntegerField(source='service.price', read_only=True, allow_null=True)
    attendees = AttendeeSerializer(source='client_links', many=True, read_only=True)
    # Null when the booking came off the public page, where there is no member
    # of staff. `source` says which case it is outright, so a reader never has
    # to infer "a stranger asked for this" from a null or from the status --
    # which would stop being true the moment anything else creates a pending row.
    created_by_name = serializers.CharField(
        source='created_by.display_name', read_only=True, default=None,
    )

    class Meta:
        model = Appointment
        fields = (
            'id', 'professional', 'professional_name', 'clients', 'attendees',
            'service', 'service_name', 'service_price', 'start', 'end', 'status', 'capacity',
            'series', 'cancelled_at', 'cancellation_reason', 'notes',
            'rescheduled_from', 'source', 'created_by', 'created_by_name',
            'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'end', 'status', 'series', 'cancelled_at', 'cancellation_reason',
            # Recorded by validate() when the start actually moves. A payload
            # that could set it could claim a booking was moved when it never was.
            'rescheduled_from',
            # Both are facts about how the row came to exist, recorded by the
            # server. A payload that could set them could dress a public request
            # up as a staff booking.
            'source', 'created_by',
            'created_at', 'updated_at',
        )

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

        # Only when the hour genuinely changes: a PATCH that sends the same
        # start, or none at all, is not a reschedule, and stamping it would put
        # a "was at 10:00" badge on a booking that has always been at 10:00.
        if self.instance is not None and start != self.instance.start:
            attrs['rescheduled_from'] = self.instance.start
            # The reminder for the OLD hour has already gone out, and
            # `reminder_sent_at` is what guarantees the sweep never revisits
            # this row -- so leaving it set would leave the professional holding
            # a notification for a time that no longer exists, permanently.
            # Clearing it puts the appointment back in front of the sweep, which
            # recomputes `start - lead` and notifies again at the new hour.
            attrs['reminder_sent_at'] = None

        self._check_capacity(attrs)
        return attrs


class AppointmentSeriesSerializer(AppointmentTemplateMixin, serializers.ModelSerializer):
    """
    Books a whole arrangement at once: the rule, plus the appointment to repeat.

    The write side carries a booking's own fields (`professional`, `clients`,
    `service`, `start`, `capacity`, `notes`) because a series is not an object
    anybody wants for itself -- it is fifty bookings somebody wants, described
    once. `start` is the FIRST occurrence, instant included; everything after it
    reuses that wall-clock time on the days the rule picks.

    The read side answers with what actually landed: `appointments`, the
    `joined` subset of them that were somebody else's class already, and, just
    as importantly, `skipped`.
    """

    # All write-only: none of them is a column on the series, they describe the
    # booking to repeat. On the way out they are already on every occurrence in
    # `appointments`, with names attached, so echoing bare ids here would be the
    # same answer twice and a lookup for the reader either way.
    professional = serializers.PrimaryKeyRelatedField(
        queryset=Membership.objects.none(), write_only=True
    )
    clients = serializers.PrimaryKeyRelatedField(
        many=True, allow_empty=False, queryset=Client.objects.none(), write_only=True
    )
    service = serializers.PrimaryKeyRelatedField(
        queryset=Service.objects.none(), write_only=True
    )
    start = serializers.DateTimeField(write_only=True)
    capacity = serializers.IntegerField(min_value=1, required=False, write_only=True)
    notes = serializers.CharField(required=False, allow_blank=True, write_only=True)

    # Everywhere this enrolment now belongs, whether the occurrence was created
    # for it or already existed. The caller asked to enrol somebody; the answer
    # is where that landed, and which half it landed by is what `joined` is for.
    appointments = AppointmentSerializer(many=True, read_only=True)
    # The days the rule asked for and the diary would not give: the professional
    # was already busy with something that is not this class. Reported instead of
    # failing the whole request, because a term of Mondays is not worth abandoning
    # over one clash in week five, and a receptionist told WHICH days to look at
    # can fix those in seconds.
    skipped = serializers.SerializerMethodField()
    # The subset of `appointments` that was already in the diary and took this
    # roster on instead of being created. Told apart from the rest so the UI can
    # say "added to 12 existing classes, created 4" -- two very different things
    # to a receptionist, and indistinguishable from `appointments` alone.
    joined = serializers.SerializerMethodField()

    class Meta:
        model = AppointmentSeries
        fields = (
            'id', 'professional', 'clients', 'service', 'start', 'capacity', 'notes',
            'frequency', 'interval', 'weekdays', 'until',
            'appointments', 'skipped', 'joined', 'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    @extend_schema_field(serializers.ListField(child=serializers.DateField()))
    def get_skipped(self, series):
        # Formatted here rather than left to the JSON renderer, so `.data` is
        # the wire format everywhere -- including for anything reading the
        # response without rendering it.
        return [day.isoformat() for day in getattr(series, 'skipped_days', [])]

    @extend_schema_field(serializers.ListField(child=serializers.DateField()))
    def get_joined(self, series):
        return [day.isoformat() for day in getattr(series, 'joined_days', [])]

    def validate_interval(self, interval):
        if interval < 1:
            raise serializers.ValidationError('Repeat at least every one week or month.')
        return interval

    def validate_weekdays(self, weekdays):
        if any(day < 0 or day > 6 for day in weekdays):
            raise serializers.ValidationError('Days run from 0 (Monday) to 6 (Sunday).')
        return sorted(set(weekdays))

    def validate(self, attrs):
        tenant = self.context['request'].tenant
        first = attrs['start'].astimezone(ZoneInfo(tenant.timezone)).date()

        if attrs['until'] < first:
            raise serializers.ValidationError(
                {'until': 'The series ends before its first appointment.'}
            )
        if attrs.get('weekdays') and attrs.get('frequency') == AppointmentSeries.Frequency.MONTHLY:
            raise serializers.ValidationError(
                {'weekdays': 'Only a weekly series repeats on chosen days.'}
            )

        # Asked before anything is written, using an unsaved row purely as the
        # rule calculator. The generator caps itself, but a silent cap is a term
        # that quietly stops in August and nobody knows why until a client turns
        # up to a class that was never booked.
        planned = AppointmentSeries(
            frequency=attrs.get('frequency', AppointmentSeries.Frequency.WEEKLY),
            interval=attrs.get('interval', 1),
            weekdays=attrs.get('weekdays', []),
            until=attrs['until'],
        ).occurrence_dates(first)
        if len(planned) >= MAX_OCCURRENCES:
            raise serializers.ValidationError(
                {'until': f'That is more than {MAX_OCCURRENCES} appointments. '
                          'Book a shorter run and renew it.'}
            )

        self._check_capacity(attrs)
        return attrs

    def create(self, validated_data):
        """
        Materialise the arrangement.

        Each occurrence is inserted in a savepoint of its own so one clash
        cannot take the rest of the term with it -- and a clash IS expected:
        the exclusion constraint is the only thing that knows the professional's
        diary, and it answers one insert at a time. A clash that turns out to be
        the same class at the same hour is enrolled into rather than skipped.
        """
        tenant = self.context['request'].tenant
        booking = {
            key: validated_data.pop(key)
            for key in ('professional', 'service', 'start', 'clients')
        }
        booking['capacity'] = validated_data.pop(
            'capacity', Appointment._meta.get_field('capacity').default
        )
        booking['notes'] = validated_data.pop('notes', '')

        series = AppointmentSeries.objects.create(tenant=tenant, **validated_data)

        # The wall-clock time, in the tenant's zone, is what repeats. Rebuilding
        # each start from a local date plus that time is what keeps a 07:00 class
        # at 07:00 after the clocks move; adding seven days to a UTC instant
        # would quietly shift half the term by an hour.
        zone = ZoneInfo(tenant.timezone)
        local_first = booking['start'].astimezone(zone)
        booked, joined, skipped = [], [], []

        for day in series.occurrence_dates(local_first.date()):
            start = datetime.combine(day, local_first.time()).replace(tzinfo=zone)
            appointment = self._book(series, booking, start)
            if appointment is None:
                appointment = self._join(series, booking, start)
                if appointment is not None:
                    joined.append(day)
            (booked if appointment is not None else skipped).append(appointment or day)

        if not booked:
            # Nothing landed at all -- neither created nor joined -- so there is
            # no arrangement, only a row claiming one. Rolled back rather than
            # returned as an empty success nobody would read. A term that joined
            # every one of its dates is not this case: the client is enrolled in
            # every class they asked for, which is the whole point.
            series.delete()
            raise serializers.ValidationError(
                'Every date in this series clashes with an existing appointment.'
            )

        series.skipped_days = skipped
        series.joined_days = joined
        series.appointments_created = booked
        return series

    @staticmethod
    def _book(series, booking, start):
        """The occurrence, or None if the professional was already busy then."""
        try:
            with transaction.atomic():
                appointment = Appointment.objects.create(
                    tenant=series.tenant,
                    series=series,
                    professional=booking['professional'],
                    service=booking['service'],
                    start=start,
                    end=start + booking['service'].duration,
                    capacity=booking['capacity'],
                    notes=booking['notes'],
                )
                AppointmentClient.objects.bulk_create(
                    AppointmentClient(appointment=appointment, client=client, series=series)
                    for client in booking['clients']
                )
        except IntegrityError as exc:
            # By constraint name: any other integrity failure here is a real
            # fault and must keep surfacing as one.
            if 'no_overlap_per_professional' not in str(exc):
                raise
            return None
        return appointment

    @staticmethod
    def _join(series, booking, start):
        """
        The class already in the diary at that hour, now carrying this roster --
        or None if the clash was a real one.

        A recurring slot in a group business is ONE class, not one class per
        client: the second person to want Mondays at 11:00 is enrolling, not
        double-booking. Without this the whole term comes back as `skipped` and
        the receptionist adds them by hand, occurrence by occurrence.

        Only an exact match is a class. `start` must be equal to the second,
        because a partial overlap is the appointment next door running long, and
        the service must match, because two different things cannot happen in the
        same room at once whatever the hour says. Only SCHEDULED qualifies:
        joining a `pending` request would put a paying client into a booking the
        shop has not accepted, and a `completed` or `cancelled` one is not a
        class anybody can still walk into.

        Nothing on the existing appointment is rewritten -- not `capacity`, not
        `notes`. It belongs to whoever booked it; an enrolment asks for a place
        in it, and the class's own ceiling is what decides whether there is one.
        """
        existing = Appointment.objects.filter(
            tenant=series.tenant,
            professional=booking['professional'],
            service=booking['service'],
            start=start,
            status=Appointment.Status.SCHEDULED,
        ).first()
        if existing is None:
            return None

        # Anyone already on the roster is not joining again -- inserting them
        # would only hit `unique_client_per_appointment` and lose the others in
        # the same statement. Dropping them also keeps the head count honest:
        # they are already inside the ceiling, not on top of it. If that empties
        # the list the join still succeeded, because the end state asked for --
        # these clients in this class -- already holds.
        enrolled = set(existing.client_links.values_list('client_id', flat=True))
        joining = [client for client in booking['clients'] if client.pk not in enrolled]
        if len(enrolled) + len(joining) > existing.capacity:
            return None

        try:
            # Its own savepoint because the row that blocked the insert can be
            # cancelled, filled or moved between that failure and this lookup,
            # and a term of Mondays must not die of one lost race.
            with transaction.atomic():
                AppointmentClient.objects.bulk_create(
                    AppointmentClient(appointment=existing, client=client, series=series)
                    for client in joining
                )
        except IntegrityError:
            return None
        return existing

    def to_representation(self, series):
        data = super().to_representation(series)
        # The prefetch-free path: create() already holds the rows it made, in
        # order, and re-reading them would only risk showing a different set.
        made = getattr(series, 'appointments_created', None)
        if made is not None:
            data['appointments'] = AppointmentSerializer(
                made, many=True, context=self.context
            ).data
        return data


class MovedFollowingSerializer(serializers.Serializer):
    """
    What a forward move actually did: the occurrences that took the new hour,
    and the days that would not, because the professional was already booked
    then. Declared so the schema says so instead of promising a bare list.
    """

    appointments = AppointmentSerializer(many=True, read_only=True)
    skipped = serializers.ListField(child=serializers.DateField(), read_only=True)


class RescheduleFollowingSerializer(serializers.Serializer):
    """
    Body of the "this one and the following" move: the new wall-clock time, and
    optionally who attends from now on.

    A time and not a datetime: what moves is the hour of a recurring class, on
    each occurrence's own day. Sending an instant would ask which day it belongs
    to and answer nothing about the other forty.
    """

    time = serializers.TimeField()
    professional = serializers.PrimaryKeyRelatedField(
        queryset=Membership.objects.none(), required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        tenant = getattr(self.context.get('request'), 'tenant', None)
        if tenant is not None:
            self.fields['professional'].queryset = Membership.professionals_for(tenant)
