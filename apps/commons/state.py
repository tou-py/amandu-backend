from django.db.models import Count, Max, Q
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Notification
from apps.scheduling.models import AgendaVersion
from apps.tenancy.permissions import HasActiveMembership


class StateSerializer(serializers.Serializer):
    """
    Two opaque tokens. A client compares them with the last pair it saw and
    refetches when one differs; nothing outside this file parses them, and their
    format is free to change without being a breaking change.
    """

    appointments = serializers.CharField(read_only=True)
    notifications = serializers.CharField(read_only=True)


class StateView(APIView):
    """
    Has anything changed -- answered without sending anything that changed.

    The agenda asks four questions on a timer (the period on screen, the week's
    load, the backlog, the queue) and the bell asks a fifth. On the overwhelming
    majority of ticks the answer to all five is "the same as last time", and that
    answer used to cost 5 requests and 21 queries, 10 of them the same user and
    membership resolved five times over.

    This is that answer in one request: a counter and an aggregate. The HTTP ETag already
    on those endpoints cannot do it: `ConditionalGetMiddleware` runs in
    `process_response`, so a 304 has already run the view and every query behind
    it -- it saves bandwidth, and bandwidth is not what runs out. Sync workers
    are, and this hands one back.

    It lives in `commons` because it crosses two apps: the diary belongs to
    `scheduling` and the feed to `accounts`.
    """

    permission_classes = (IsAuthenticated, HasActiveMembership)

    @extend_schema(responses=StateSerializer)
    def get(self, request):
        # One row, moved by triggers on every table the agenda reads (see
        # AgendaVersion). It replaced COUNT and MAX(updated_at) over turns, which
        # missed every change that lives off the turn row: attendance, payments,
        # plans, client names. Tenant-wide, not per range: a slot moved into next
        # week has to invalidate this week too.
        diary = AgendaVersion.objects.filter(tenant=request.tenant).values_list('version', flat=True).first() or 0

        # Per membership, not per tenant: this feed is one person's, and the
        # unread count is what the bell draws. `read_at` turning non-null is a
        # change the other two numbers cannot see -- marking everything read
        # moves no row count and no id.
        feed = Notification.objects.filter(recipient=request.membership).aggregate(
            rows=Count('id'),
            latest=Max('id'),
            unread=Count('id', filter=Q(read_at__isnull=True)),
        )

        return Response(
            {
                'appointments': f'v{diary}',
                'notifications': f'{feed["rows"]}:{feed["latest"] or ""}:{feed["unread"]}',
            }
        )
