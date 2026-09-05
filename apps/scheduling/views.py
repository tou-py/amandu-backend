from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated

# Safe in this direction only: accounting names scheduling by string and never
# imports it, so there is no cycle to make here. Imported at all because taking a
# month of a client's plan is ONE event at the counter -- the cover is extended
# and the money is booked -- and two requests for it is what leaves a day's
# takings disagreeing with its plans.
from apps.accounting.models import CashEntry
from apps.accounts.models import Membership, Notification
from apps.commons.dates import one_month_after
from apps.commons.mixins import NoHeuristicCacheMixin
from apps.scheduling.models import (
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
from apps.scheduling.permissions import OwnsAppointmentOrActsForTheTeam
from apps.scheduling.serializers import (
    AppointmentCancelSerializer,
    AppointmentSeriesSerializer,
    AppointmentSerializer,
    AttendanceSerializer,
    CategorySerializer,
    ClientFieldSerializer,
    ClientSerializer,
    DayLoadSerializer,
    MovedFollowingSerializer,
    ProfessionalSerializer,
    RescheduleFollowingSerializer,
    ServiceSerializer,
    TimeOffSerializer,
    VisitSerializer,
    WorkScheduleSerializer,
)
from apps.scheduling.workload import daily_load
from apps.tenancy.permissions import HasActiveMembership, IsTenantAdmin
from apps.tenancy.viewsets import TenantScopedModelViewSet


class Overlaps(APIException):
    """
    409, not the serializer's 400: nothing was wrong with the request when it was
    made. validate() looked and the slot WAS free -- another transaction took it
    between that look and this insert. That is state, not input, which is the
    same distinction Referenced draws for a protected delete.

    Deliberately the same sentence the serializer raises when it does see the
    clash: one rule, one message, whichever of the two paths gets there first.
    The frontend matches on that text (errors.ts, translateAppointment), so a
    reword here has to be made in both places.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = 'This professional already has an appointment in that time range.'
    default_code = 'overlaps'


class ClientViewSet(TenantScopedModelViewSet):
    """The client file: who they are, whatever this tenant asks about them, and
    every appointment they have ever been on the roster of."""

    queryset = Client.objects.all()
    serializer_class = ClientSerializer

    @extend_schema(request=None, responses=ClientSerializer)
    @action(detail=True, methods=['post'], url_path='register-payment')
    def register_payment(self, request, pk=None):
        """
        A month of this client's plan, paid for at the counter.

        Extends from whichever is later, today or the day already covered: paying
        early stacks onto what is left instead of throwing it away, and paying
        late starts today rather than back-dating time nobody could use. That is
        the same rule the platform applies to a tenant, out of the same helper --
        the month clamp is the part nobody gets right twice.

        No role check, deliberately. The cash BOOK is owner/admin because the
        shop's aggregate takings are the protected thing; taking one client's
        monthly fee is what the front desk is there to do.
        """
        client = self.get_object()
        if client.monthly_fee is None:
            raise ValidationError(
                {'monthly_fee': 'This client has no monthly plan, so there is nothing to charge.'}
            )

        today = timezone.localdate()
        # Atomic because these are two halves of one fact. A cover extended
        # without its entry is a month the shop gave away and cannot see in the
        # book; an entry without the extension is money taken for nothing.
        with transaction.atomic():
            client.paid_until = one_month_after(max(client.paid_until or today, today))
            client.save(update_fields=['paid_until', 'updated_at'])
            CashEntry.objects.create(
                tenant=request.tenant,
                kind=CashEntry.Kind.INCOME,
                amount=client.monthly_fee,
                occurred_on=today,
                # Spanish, unlike everything around it: this string is not an
                # identifier, it is the line the shop reads in its cash book,
                # sitting between entries the front end writes in Spanish. The
                # separator matches those too.
                concept=f'Mensualidad · {client.name} · hasta {client.paid_until:%d/%m/%Y}',
                # No appointment: a plan is paid for the month, not for a slot.
            )

        return Response(self.get_serializer(client).data)

    @extend_schema(responses=VisitSerializer(many=True))
    @action(detail=True, methods=['get'])
    def timeline(self, request, pk=None):
        """
        This client's visits, most recent first, with what was written down on
        each one. Future bookings included: the receptionist opening the file
        wants to know the next appointment as much as the last one.

        A read over rows that already exist -- no new storage. What a visit note
        is today is `Appointment.notes`, one text field per slot, shared by the
        group in it. A note per person, with an author and its own timestamp,
        stays deferred until somebody needs to know who wrote what.

        Paginated: a client of three years has hundreds of these.
        """
        client = self.get_object()
        visits = (
            AppointmentClient.objects
            # Scoped by BOTH sides on purpose. get_object() already proved the
            # client is this tenant's, but a relation traversed from here reaches
            # the appointment table unscoped (TenantOwnedMixin, rule 3), and this
            # is a client file -- the one place a leak would be read as history.
            .filter(client=client, appointment__tenant=request.tenant)
            .select_related('appointment__service', 'appointment__professional__user')
            .order_by('-appointment__start')
        )
        page = self.paginate_queryset(visits)
        serializer = VisitSerializer(page if page is not None else visits, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class ClientFieldViewSet(TenantScopedModelViewSet):
    """
    What this tenant asks about its clients, over and above name and phone.

    Reading is open to any active membership -- the client form cannot be drawn
    without it -- while defining, renaming and removing a field is an owner/admin
    decision: it reshapes the form for the whole business, and a delete throws
    away every answer already given.
    """

    queryset = ClientField.objects.all()
    serializer_class = ClientFieldSerializer
    # Bounded by how many questions a business asks about a client, and read to
    # build a form, so a second page would render half of it.
    pagination_class = None

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.request.method not in SAFE_METHODS:
            permissions.append(IsTenantAdmin())
        return permissions

    def perform_destroy(self, instance):
        """
        Take the answers with the question.

        Left behind, they are keys no definition explains: ClientSerializer
        rejects unknown keys, so every later edit of those clients would 400 on
        data the operator never typed and cannot see. Deleting a field is
        deliberate and rare, and it has to leave the files consistent.

        ponytail: one UPDATE per client holding the key. `has_key` keeps that to
        the clients actually affected; batch it if a tenant ever has enough of
        them for this to be felt.
        """
        with transaction.atomic():
            holders = Client.objects.for_tenant(instance.tenant).filter(
                custom_data__has_key=instance.key
            )
            for client in holders:
                del client.custom_data[instance.key]
                client.save(update_fields=['custom_data', 'updated_at'])
            super().perform_destroy(instance)


class AppointmentSeriesViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    Recurring arrangements: every Monday and Wednesday at seven, a control every
    six months.

    Create only, plus reading back what a rule produced. There is deliberately
    no update and no delete here, and that is the design rather than a gap: the
    appointments are the truth and the rule is a record of what was asked for.
    Changing the arrangement means acting on the bookings -- `cancel-following`
    and `reschedule-following` on an occurrence -- so that a rewritten rule can
    never disagree with the diary anybody is actually reading.
    """

    permission_classes = (IsAuthenticated, HasActiveMembership)
    queryset = AppointmentSeries.objects.prefetch_related(
        'appointments__professional__user',
        'appointments__service',
        'appointments__client_links__client',
    )
    serializer_class = AppointmentSeriesSerializer

    def get_queryset(self):
        return super().get_queryset().for_tenant(self.request.tenant)


class CategoryViewSet(TenantScopedModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class ServiceViewSet(TenantScopedModelViewSet):
    queryset = Service.objects.all()
    serializer_class = ServiceSerializer


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                'professional', int,
                description="Only this person's week. Omitted, the whole team's.",
            ),
        ],
    ),
)
class WorkScheduleViewSet(TenantScopedModelViewSet):
    """
    The recurring week: when each professional is normally at work.

    Readable by any active membership, because the agenda and the availability
    calculation both need it. Writable only by owner/admin: these rows decide
    what the public page offers to strangers, so widening them is a business
    decision, not a personal one.
    """

    queryset = WorkSchedule.objects.all()
    serializer_class = WorkScheduleSerializer
    # Bounded by staff times days of the week, and read whole to draw the week.
    pagination_class = None

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.request.method not in SAFE_METHODS:
            permissions.append(IsTenantAdmin())
        return permissions

    def get_queryset(self):
        rows = super().get_queryset()
        professional = self.request.query_params.get('professional')
        if professional:
            rows = rows.filter(professional_id=professional)
        return rows


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter('from', OpenApiTypes.DATE, description='YYYY-MM-DD, inclusive.'),
            OpenApiParameter('to', OpenApiTypes.DATE, description='YYYY-MM-DD, inclusive.'),
        ],
    ),
)
class TimeOffViewSet(TenantScopedModelViewSet):
    """
    Holidays, closures and absences: what comes out of the working week.

    Same split as the schedule, for the same reason -- except that a row here
    only ever REMOVES availability, which is why it is the one an owner reaches
    for in a hurry and why it stays cheap to write.
    """

    queryset = TimeOff.objects.all()
    serializer_class = TimeOffSerializer

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.request.method not in SAFE_METHODS:
            permissions.append(IsTenantAdmin())
        return permissions

    def get_queryset(self):
        """
        Defaults to what is still ahead. A shop opening this list wants the
        closures it has to plan around, not every sick day since it opened --
        and past rows only grow.
        """
        rows = super().get_queryset()
        tz = ZoneInfo(self.request.tenant.timezone)
        since = self._parse_day(self.request.query_params.get('from'), tz, 'from')
        until = self._parse_day(self.request.query_params.get('to'), tz, 'to')

        if since is None and until is None:
            return rows.filter(end__gte=timezone.now())
        if since is not None:
            rows = rows.filter(end__gt=since)
        if until is not None:
            rows = rows.filter(start__lt=until + timedelta(days=1))
        return rows

    @staticmethod
    def _parse_day(value, tz, field):
        if value is None:
            return None
        try:
            return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=tz)
        except ValueError:
            raise ValidationError({field: 'Use YYYY-MM-DD.'})


class ProfessionalViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    The people this tenant's agenda can book, so a client can label and colour a
    slot without asking who each `professional` id belongs to.

    Not a TenantScopedModelViewSet: that base re-scopes through TenantOwnedMixin's
    `for_tenant`, and Membership has no such manager -- it IS the tenant link.
    The scoping is therefore written out here.

    List only. There is no detail route because nothing needs one: the agenda
    reads the whole list once to resolve ids, and memberships are created by
    invitation, never here.

    Unpaginated on purpose. This is a reference list read to resolve ids, so a
    truncated first page would silently mislabel every slot belonging to the
    professionals on page two. It is bounded by a business's staff, not by data.
    """

    serializer_class = ProfessionalSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership)
    pagination_class = None

    def get_queryset(self):
        return (
            Membership.professionals_for(self.request.tenant)
            .select_related('user')
            .order_by('user__first_name', 'user__email')
        )


class AgendaPagination(PageNumberPagination):
    """
    Lets the agenda ask for its whole week in one response.

    The project default of 50 turns a busy shop's week into five sequential
    round trips -- five times the JWT check, the tenant lookup and the throttle
    hit -- to draw one screen. But the backlog and the request queue deliberately
    read only page one, so raising the default for everybody would hand them a
    payload five times larger for no gain. Hence a parameter rather than a new
    default: the grid asks, nothing else has to.

    `max_page_size` is the actual guard. The agenda's range is bounded by the
    calendar, so a big page is safe there; the cap is what stops the parameter
    being a way to ask for an unbounded scan.
    """

    page_size_query_param = 'page_size'
    max_page_size = 300


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                'from',
                OpenApiTypes.DATE,
                description='First calendar day to include (YYYY-MM-DD), read in the '
                            'tenant timezone. Inclusive.',
            ),
            OpenApiParameter(
                'to',
                OpenApiTypes.DATE,
                description='Last calendar day to include (YYYY-MM-DD), read in the '
                            'tenant timezone. Inclusive of the whole day.',
            ),
            OpenApiParameter(
                'professional',
                OpenApiTypes.INT,
                description='Membership id of the professional attending.',
            ),
            OpenApiParameter(
                 'status',
                OpenApiTypes.STR,
                enum=Appointment.Status.values,
                description='Only appointments in this status.',
            ),
        ],
    ),
)
class AppointmentViewSet(NoHeuristicCacheMixin, TenantScopedModelViewSet):
    """
    The agenda: booking slots, moving them, and closing them out.

    A docstring of its own is not decoration. drf-spectacular describes an
    endpoint with `inspect.getdoc(view)`, which walks the MRO -- so without one
    here the public API documentation showed whatever the first base class
    happened to say about its own internals. That is how a note about
    Cache-Control ended up describing "list appointments".
    """

    permission_classes = (IsAuthenticated, HasActiveMembership, OwnsAppointmentOrActsForTheTeam)
    pagination_class = AgendaPagination
    queryset = (
        Appointment.objects
        # `created_by__user` is not optional here, it is an N+1 fix. The
        # serializer reads `created_by.display_name`, which walks TWO relations,
        # so without this every row costs two extra queries: 50 rows went from 5
        # queries to 105. It hid behind the seed data, where `created_by` is
        # null and DRF short-circuits to the field default -- every appointment
        # booked through the API has it set, so only real use showed it.
        .select_related('professional__user', 'service', 'created_by__user')
        # The links, not the clients: the serializer reads attendance off the
        # through row, and prefetching only `clients` would query it per slot.
        .prefetch_related('client_links__client')
    )
    serializer_class = AppointmentSerializer

    # The widest range `summary` will aggregate. A screen asks for a week, or a
    # month at the outside; past a year the request stops being a view of the
    # diary and becomes a walk of the whole table.
    MAX_SUMMARY_DAYS = 366

    def get_queryset(self):
        """
        The agenda, filtered. `from`/`to` are calendar days (YYYY-MM-DD) read in
        the tenant timezone, not UTC (R13): a 23:00 local appointment stored as
        the next UTC day still belongs to its local day. Both ends inclusive.
        """
        queryset = super().get_queryset()
        params = self.request.query_params
        tz = ZoneInfo(self.request.tenant.timezone)

        professional = params.get('professional')
        if professional is not None:
            if not professional.isdigit():
                raise ValidationError({'professional': 'Must be a membership id.'})
            queryset = queryset.filter(professional_id=professional)

        status = params.get('status')
        if status is not None:
            if status not in Appointment.Status.values:
                raise ValidationError({'status': f'Must be one of {Appointment.Status.values}.'})
            queryset = queryset.filter(status=status)

        day_from = self._parse_day(params.get('from'), tz, 'from')
        if day_from is not None:
            queryset = queryset.filter(start__gte=day_from)

        day_to = self._parse_day(params.get('to'), tz, 'to')
        if day_to is not None:
            # Inclusive of the whole 'to' day: up to the next local midnight.
            queryset = queryset.filter(start__lt=day_to + timedelta(days=1))

        return queryset

    @extend_schema(
        parameters=[
            OpenApiParameter(
                'from',
                OpenApiTypes.DATE,
                required=True,
                description='First local day to include (YYYY-MM-DD). Inclusive.',
            ),
            OpenApiParameter(
                'to',
                OpenApiTypes.DATE,
                required=True,
                description='Last local day to include (YYYY-MM-DD). Inclusive.',
            ),
        ],
        responses=DayLoadSerializer(many=True),
    )
    # `pagination_class=None` is not decoration: the router's paginator applies
    # to every list-shaped route on the viewset, so drf-spectacular described
    # this one as a paged envelope -- count/next/previous/results -- while the
    # handler below returns a bare list. The generated client believed the
    # schema. Seven rows never need a second page anyway.
    @action(detail=False, methods=['get'], pagination_class=None)
    def summary(self, request):
        """
        How full each day of the range is: one row per day instead of every slot
        on it.

        Why it exists. The agenda's week strip needs fourteen integers -- a count
        and a busy-minutes figure for each of seven days -- and the only way to
        get them was to page through the whole week on the list endpoint. For a
        shop with a couple of hundred bookings a week that is five requests every
        thirty seconds, each carrying full slots with their service, professional
        and roster attached, so the client can add them up and throw the rows
        away. This is one query over two columns.

        Days with nothing on them are absent rather than sent as zeros: the
        caller is drawing a fixed row of days and already knows which ones it
        asked for, so a day missing here means the same thing a zero would, in
        fewer bytes.

        Cancelled slots are out -- they held nothing. Pending ones are IN: a
        request nobody has answered still holds its hour, which is exactly what
        the overlap constraint says about it.
        """
        params = request.query_params
        if not params.get('from') or not params.get('to'):
            raise ValidationError('Both `from` and `to` are required.')

        tz = ZoneInfo(request.tenant.timezone)
        day_from = self._parse_day(params['from'], tz, 'from')
        day_to = self._parse_day(params['to'], tz, 'to')
        if day_to < day_from:
            raise ValidationError({'to': 'Cannot be before `from`.'})
        # A screen shows a week, a month at the very most. Anything past a year
        # is a scrape, and it would walk every appointment the tenant has.
        if (day_to - day_from).days > self.MAX_SUMMARY_DAYS:
            raise ValidationError(
                {'to': f'Ask for at most {self.MAX_SUMMARY_DAYS} days at a time.'}
            )

        spans = (
            self.get_queryset()
            .exclude(status=Appointment.Status.CANCELLED)
            # Two columns and no model instances. The class queryset carries a
            # select_related and a prefetch for the serializer's benefit, and
            # this endpoint reads neither a service, nor a professional, nor a
            # roster -- values_list leaves all three unpaid for.
            .values_list('start', 'end')
        )
        return Response(DayLoadSerializer(daily_load(spans, tz), many=True).data)

    # AppointmentSerializer.validate() checks for a clash and cannot hold what it
    # found free: between its .exists() and the INSERT, another transaction can
    # book the same range. no_overlap_per_professional (appointment.py) is what
    # actually stops the double booking, and uncaught it reached the handler as
    # an IntegrityError -- a 500 telling two receptionists working at once that
    # the server broke, when the correct answer is "somebody beat you to it".
    #
    # Both hooks, and only these two: cancel() takes a row OUT of the constraint's
    # condition and complete() leaves its range untouched, so neither can raise it.
    def perform_create(self, serializer):
        # Who booked it, recorded here and never taken from the payload. The
        # public page leaves this null and says so through `source`, so the two
        # ways a slot can enter the diary stay told apart.
        self._save_or_conflict(
            lambda s: s.save(tenant=self.request.tenant, created_by=self.request.membership),
            serializer,
        )

    def perform_update(self, serializer):
        self._save_or_conflict(super().perform_update, serializer)

    @staticmethod
    def _save_or_conflict(save, serializer):
        try:
            # atomic() and not a bare try: a failed statement marks the whole
            # surrounding transaction for rollback, so catching the error and
            # carrying on is only safe from inside a savepoint of its own. It
            # costs nothing today (no ATOMIC_REQUESTS, so this IS the
            # transaction) and is what keeps this correct if that ever changes
            # or a caller wraps a batch of bookings -- which §2.4's recurring
            # series will.
            with transaction.atomic():
                save(serializer)
        except IntegrityError as exc:
            # By constraint name: any other integrity failure here is a real
            # fault and has to keep surfacing as one instead of being dressed up
            # as an ordinary scheduling clash.
            if 'no_overlap_per_professional' not in str(exc):
                raise
            raise Overlaps() from exc

    @staticmethod
    def _parse_day(value, tz, field):
        if value is None:
            return None
        try:
            return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=tz)
        except ValueError:
            raise ValidationError({field: 'Use YYYY-MM-DD.'})

    def _transition(self, appointment, apply):
        try:
            apply()
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages)
        return Response(self.get_serializer(appointment).data)

    @extend_schema(request=AppointmentCancelSerializer, responses=AppointmentSerializer)
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        appointment = self.get_object()
        # Validated rather than read raw off request.data: `reason` is free text
        # that lands in the record, so it goes through a field like any other.
        body = AppointmentCancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_data.get('reason', '')
        response = self._transition(appointment, lambda: appointment.cancel(reason))
        # Only a teammate cancelling on someone else's behalf is news to the
        # professional -- cancelling your own slot is not something you need to
        # be told about.
        if request.membership.id != appointment.professional_id:
            Notification.objects.create(
                recipient=appointment.professional,
                actor=request.membership,
                appointment=appointment,
                verb=Notification.Verb.APPOINTMENT_CANCELLED,
            )
        return response

    # No body: the URL already names the transition.
    @extend_schema(request=None, responses=AppointmentSerializer)
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        appointment = self.get_object()
        return self._transition(appointment, appointment.complete)

    # No body: the URL already names the transition.
    @extend_schema(request=None, responses=AppointmentSerializer)
    @action(detail=True, methods=['post'])
    def confirm(self, request, pk=None):
        """
        The shop accepting a request that came off the public page.

        Turning one down has no action of its own: that is `cancel`, which is
        already the transition that gives a slot back and already carries the
        reason the shop typed.
        """
        appointment = self.get_object()
        return self._transition(appointment, appointment.confirm)

    def _following(self, appointment):
        """
        This occurrence and every later one in the same arrangement.

        From `start` and not from the id: an occurrence moved to another day is
        still where it now sits, and "the following ones" means the ones that
        come after it in time. Cancelled ones are already out of the way, and a
        completed one is history nothing may rewrite.
        """
        return (
            Appointment.objects.for_tenant(self.request.tenant)
            .filter(
                series_id=appointment.series_id,
                start__gte=appointment.start,
                status=Appointment.Status.SCHEDULED,
            )
            # The same related loading the class queryset carries, and for the
            # same reason: both actions below answer with AppointmentSerializer,
            # which reads the professional, whoever booked it and the roster. On
            # `service` alone a forty-week arrangement cost four queries per
            # occurrence to serialise the answer.
            .select_related('professional__user', 'service', 'created_by__user')
            .prefetch_related('client_links__client')
            .order_by('start')
        )

    @staticmethod
    def _require_series(appointment):
        if appointment.series_id is None:
            raise ValidationError('This appointment is not part of a series.')

    @extend_schema(request=AppointmentCancelSerializer, responses=AppointmentSerializer(many=True))
    @action(detail=True, methods=['post'], url_path='cancel-following')
    def cancel_following(self, request, pk=None):
        """
        Cancel this occurrence and the rest of the arrangement from here on.

        The client left the term; the past stays exactly as it happened. Earlier
        occurrences are untouched, and so is any later one already cancelled or
        completed.
        """
        appointment = self.get_object()
        self._require_series(appointment)

        body = AppointmentCancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_data.get('reason', '')

        cancelled = list(self._following(appointment))
        for one in cancelled:
            one.cancel(reason)

        # ONE notification for the whole run, not one per occurrence: the
        # professional needs to know the arrangement ended, and forty rows
        # saying so is a bell nobody will ever open again.
        if cancelled and request.membership.id != appointment.professional_id:
            Notification.objects.create(
                recipient=appointment.professional,
                actor=request.membership,
                appointment=appointment,
                verb=Notification.Verb.APPOINTMENT_CANCELLED,
            )

        return Response(self.get_serializer(cancelled, many=True).data)

    @extend_schema(
        request=RescheduleFollowingSerializer,
        responses=MovedFollowingSerializer,
    )
    @action(detail=True, methods=['post'], url_path='reschedule-following')
    def reschedule_following(self, request, pk=None):
        """
        Move this occurrence and the rest to a new time of day, keeping each on
        its own date. Optionally hand them to another professional.

        A clash leaves that one occurrence where it was rather than failing the
        move: the class changed hour, and the one week the room was already
        taken is a thing the receptionist has to see, not a reason to abandon
        the other thirty-nine. Which ones stayed behind is in `skipped`.
        """
        appointment = self.get_object()
        self._require_series(appointment)

        body = RescheduleFollowingSerializer(data=request.data, context={'request': request})
        body.is_valid(raise_exception=True)
        professional = body.validated_data.get('professional')
        if professional is not None and professional != request.membership:
            if not request.membership.can_schedule_for_others():
                raise ValidationError(
                    'Your role only allows booking appointments for yourself.'
                )

        zone = ZoneInfo(request.tenant.timezone)
        new_time = body.validated_data['time']
        moved, skipped = [], []

        for one in self._following(appointment):
            was_at = one.start
            local_day = was_at.astimezone(zone).date()
            # Rebuilt from the local date plus the new wall-clock time, the same
            # way the series was generated: adding an offset to a UTC instant
            # would move an occurrence on the far side of a DST boundary to the
            # wrong hour.
            one.start = datetime.combine(local_day, new_time).replace(tzinfo=zone)
            one.end = one.start + one.service.duration
            if professional is not None:
                one.professional = professional
            # Handing the series to someone else without changing the hour moves
            # nobody's day, so it leaves the badge off.
            if one.start != was_at:
                one.rescheduled_from = was_at
                # Same reason as the single-appointment path, and this is where
                # it bites hardest: one POST moves an entire run, so a term of
                # Mondays could go silent all at once, on exactly the imminent
                # occurrences where a wrong reminder costs the most.
                one.reminder_sent_at = None
            try:
                with transaction.atomic():
                    one.save(update_fields=[
                        'start', 'end', 'professional', 'rescheduled_from',
                        'reminder_sent_at', 'updated_at',
                    ])
            except IntegrityError as exc:
                if 'no_overlap_per_professional' not in str(exc):
                    raise
                skipped.append(local_day)
                continue
            moved.append(one)

        return Response({
            'appointments': self.get_serializer(moved, many=True).data,
            'skipped': [day.isoformat() for day in skipped],
        })

    @extend_schema(request=AttendanceSerializer, responses=AppointmentSerializer)
    @action(detail=True, methods=['post'])
    def attendance(self, request, pk=None):
        """
        Record whether ONE person in the slot turned up. Deliberately not a
        transition on the appointment: a booking for four has four answers, and
        the booking's own status has nothing to say about any of them.
        """
        appointment = self.get_object()
        body = AttendanceSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        # .filter on the related manager, not the prefetched cache, so an id that
        # belongs to another appointment cannot be silently accepted.
        link = appointment.client_links.filter(client_id=body.validated_data['client']).first()
        if link is None:
            raise ValidationError({'client': 'That client is not in this appointment.'})

        try:
            link.mark(body.validated_data['attendance'])
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages)

        # Re-read: the instance fetched above carries a prefetched client_links
        # cache still holding the value that was just replaced.
        return Response(self.get_serializer(self.get_object()).data)
