from datetime import timedelta

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.accounting.models import CashEntry
from apps.commons.errors import Refused
from apps.invoicing import service
from apps.invoicing.models import Invoice, InvoicingAccount
from apps.invoicing.providers import (
    InvalidDocument,
    Rejected,
    Unavailable,
    get_provider,
)
from apps.invoicing.serializers import (
    InvoiceSerializer,
    InvoicingAccountSerializer,
    IssueSerializer,
    PdfQuerySerializer,
    ToIssueSerializer,
)
from apps.tenancy.permissions import (
    HasActiveMembership,
    IsTenantAdmin,
    IsTenantCoordinator,
)
from apps.tenancy.viewsets import TenantScopedModelViewSet


class InvoicingAccountView(RetrieveUpdateAPIView):
    """
    The business's account at its invoicing provider: which provider, its key
    (write-only), and the establishment, point of issue and next number it
    issues under. A singleton like /api/tenant/.

    Before it is first saved it reads as the defaults with `has_credentials`
    false, so the screen has something to fill in.
    """

    serializer_class = InvoicingAccountSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantAdmin)
    http_method_names = ('get', 'put', 'patch', 'options')

    def get_object(self):
        tenant = self.request.tenant
        return (
            InvoicingAccount.objects.filter(tenant=tenant).first()
            or InvoicingAccount(tenant=tenant, provider='sifensis')
        )


# How far back "Para facturar" looks. A month: an invoice is asked for at the
# counter or within days, and anything older is noise on a work list.
TO_ISSUE_DAYS = 30


class InvoiceViewSet(TenantScopedModelViewSet):
    """
    Electronic invoices for incomes in the cash book. POST issues one for a
    payment; the provider settles it asynchronously, and the sweep (or
    /refresh/) follows it until it is approved or rejected.
    """

    queryset = Invoice.objects.all()
    serializer_class = InvoiceSerializer
    http_method_names = ('get', 'post', 'options')
    # Same desk as taking the money: whoever charged can hand over the invoice.
    permission_classes = (*TenantScopedModelViewSet.permission_classes, IsTenantCoordinator)

    def get_queryset(self):
        invoices = super().get_queryset().select_related('cash_entry__client')
        for key in ('cash_entry', 'status'):
            value = self.request.query_params.get(key)
            if value:
                invoices = invoices.filter(**{key: value})
        return invoices

    @extend_schema(request=IssueSerializer, responses={201: InvoiceSerializer})
    def create(self, request, *args, **kwargs):
        body = IssueSerializer(data=request.data, context={'request': request})
        body.is_valid(raise_exception=True)
        data = body.validated_data
        invoice = service.issue(
            data['cash_entry'], request.user,
            tax_id=data.get('tax_id'), tax_name=data.get('tax_name'),
            save_to_client=data['save_to_client'],
        )
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[
            OpenApiParameter('cash_entry', OpenApiTypes.INT, description='Only the invoice of this payment.'),
            OpenApiParameter('status', OpenApiTypes.STR, enum=Invoice.Status.values),
        ],
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses=ToIssueSerializer(many=True))
    @action(detail=False, methods=['get'], url_path='to-issue', pagination_class=None)
    def to_issue(self, request):
        """
        The desk's work list: incomes of the last TO_ISSUE_DAYS days with no
        invoice yet, or whose invoice was rejected, newest first. Read by the
        front desk, which cannot read the cash book itself -- this is only the
        slice of it that invoicing needs.
        """
        since = timezone.localdate() - timedelta(days=TO_ISSUE_DAYS)
        entries = (
            CashEntry.objects.for_tenant(request.tenant)
            .filter(kind=CashEntry.Kind.INCOME, voided_at__isnull=True, occurred_on__gte=since)
            .filter(Q(invoice__isnull=True) | Q(invoice__status=Invoice.Status.REJECTED))
            .select_related('client', 'invoice')
            .order_by('-occurred_on', '-id')
        )
        return Response(ToIssueSerializer(entries, many=True).data)

    @extend_schema(request=None, responses=InvoiceSerializer)
    @action(detail=True, methods=['post'])
    def refresh(self, request, pk=None):
        """Ask the provider now instead of waiting for the sweep."""
        return Response(InvoiceSerializer(service.refresh(self.get_object())).data)

    @extend_schema(
        parameters=[PdfQuerySerializer],
        responses={(200, 'application/pdf'): OpenApiTypes.BINARY},
    )
    @action(detail=True, methods=['get'])
    def pdf(self, request, pk=None):
        """
        The KuDE, the printable invoice. Proxied rather than linked: the
        provider wants the business's key on the request, and that key never
        reaches a browser.
        """
        invoice = self.get_object()
        query = PdfQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        if not invoice.reference:
            raise Refused('The invoice has not been issued yet.', 'invoice_pending', status.HTTP_409_CONFLICT)
        account = InvoicingAccount.objects.get(tenant=invoice.tenant)
        try:
            content = get_provider(invoice.provider).pdf(
                account.credentials, invoice.reference, query.validated_data['layout'],
            )
        except Unavailable as exc:
            raise Refused('The invoicing provider is not answering; try again shortly.',
                          'provider_unavailable', status.HTTP_503_SERVICE_UNAVAILABLE) from exc
        except (Rejected, InvalidDocument) as exc:
            raise Refused(str(exc), 'provider_rejected', status.HTTP_502_BAD_GATEWAY) from exc
        response = HttpResponse(content, content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="factura-{invoice}.pdf"'
        return response
