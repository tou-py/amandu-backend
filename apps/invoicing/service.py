"""
Issuing, following and cancelling an invoice. Orchestration only: every word
that reaches the provider goes through apps.invoicing.providers, so nothing
here knows which provider it is.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework import status

from apps.accounting.models import CashEntry
from apps.commons.errors import Refused
from apps.invoicing.models import Invoice, InvoicingAccount
from apps.invoicing.providers import (
    Customer,
    Draft,
    InvalidDocument,
    Line,
    NumberTaken,
    Outcome,
    Rejected,
    Unavailable,
    get_provider,
)

# The SET accepts the cancellation of an approved invoice within 48 hours.
CANCEL_WINDOW = timedelta(hours=48)
# ponytail: every service at the general rate. A per-service rate when a
# business sells something exempt or at 5%.
VAT_RATE = 10
# How long the sweep keeps asking about an unsettled invoice before leaving it
# to a person.
FOLLOW_FOR = timedelta(days=7)


def _actor(user):
    return str(user.pk), user.get_full_name().strip() or user.email


def issue(entry, user, tax_id=None, tax_name=None, save_to_client=False):
    if entry.kind != CashEntry.Kind.INCOME:
        raise Refused('Only an income can be invoiced.', 'not_income')
    if entry.voided_at is not None:
        raise Refused('This payment is void.', 'voided')

    client = entry.client
    customer = Customer(
        tax_id=(tax_id if tax_id is not None else client and client.tax_id or '').strip(),
        name=(tax_name if tax_name is not None else client and (client.tax_name or client.name) or '').strip(),
        email=client.email if client else '',
    )
    if customer.tax_id and not customer.name:
        raise Refused('A tax id needs the name it belongs to.', 'tax_name_required')

    with transaction.atomic():
        # Serialises numbering per business. ponytail: the lock is held across
        # the provider call, which is fine at a shop's volume; reserve the
        # number in its own transaction if a tenant ever invoices concurrently
        # at scale.
        account = (
            InvoicingAccount.objects.select_for_update()
            .filter(tenant=entry.tenant, active=True).first()
        )
        if account is None or not account.credentials:
            raise Refused('Invoicing is not set up for this business.', 'invoicing_not_configured',
                          status.HTTP_409_CONFLICT)
        # After the lock, so two clicks on "Facturar" are one invoice and a 409.
        retry = Invoice.objects.filter(cash_entry=entry).first()
        if retry is not None and retry.status != Invoice.Status.REJECTED:
            raise Refused('This payment is already invoiced.', 'already_invoiced', status.HTTP_409_CONFLICT)
        # A rejected invoice is no fiscal document: the payment is invoiced
        # again, corrected, under the number the rejection left unused.
        establishment, point, number = (
            (retry.establishment, retry.point, retry.number) if retry
            else (account.establishment, account.point, account.next_number)
        )

        issued_by_id, issued_by_name = _actor(user)
        draft = Draft(
            establishment=establishment,
            point=point,
            number=number,
            issued_at=timezone.now(),
            customer=customer,
            lines=(Line(description=entry.concept, quantity=1, unit_price=entry.amount, vat_rate=VAT_RATE),),
            payment_method=entry.payment_method,
            issued_by_id=issued_by_id,
            issued_by_name=issued_by_name,
        )
        invoice = retry or Invoice(
            tenant=entry.tenant, cash_entry=entry, establishment=establishment, point=point, number=number,
        )
        invoice.provider = account.provider
        invoice.draft = draft.to_json()
        invoice.total = draft.total
        invoice.customer_tax_id = customer.tax_id
        invoice.customer_name = customer.name or 'Sin Nombre'
        invoice.issued_by = user
        try:
            _apply(invoice, get_provider(account.provider).issue(account.credentials, draft))
        except InvalidDocument as exc:
            # Nothing was issued: roll back and leave the number for the next one.
            raise Refused(
                {'detail': 'The invoicing provider refused the document.', 'errors': exc.errors},
                'invalid_invoice',
            ) from exc
        except NumberTaken as exc:
            raise Refused(
                f'Invoice number {draft.number} is already used at the provider; '
                "move the account's next number past it.",
                'invoice_number_taken', status.HTTP_409_CONFLICT,
            ) from exc
        except Rejected as exc:
            raise Refused(str(exc), 'provider_rejected', status.HTTP_502_BAD_GATEWAY) from exc
        except Unavailable as exc:
            # Maybe issued. The number is spent either way; the sweep re-sends.
            invoice.status = Invoice.Status.SENDING
            invoice.message = str(exc)
        invoice.save()
        if retry is None:
            account.next_number += 1
            account.save(update_fields=['next_number', 'updated_at'])

        if save_to_client and client is not None:
            client.tax_id, client.tax_name = customer.tax_id, customer.name
            client.save(update_fields=['tax_id', 'tax_name', 'updated_at'])
    return invoice


def _apply(invoice, outcome: Outcome):
    invoice.reference = outcome.reference
    invoice.status = outcome.status
    invoice.message = outcome.message
    if outcome.status == Invoice.Status.APPROVED and invoice.approved_at is None:
        invoice.approved_at = outcome.approved_at or timezone.now()


def refresh(invoice):
    """Ask the provider again about an invoice that has not settled. Never raises
    for the provider being away: the next sweep will ask again."""
    if invoice.status not in (Invoice.Status.SENDING, Invoice.Status.PENDING):
        return invoice
    account = InvoicingAccount.objects.get(tenant=invoice.tenant)
    provider = get_provider(invoice.provider)
    try:
        if invoice.status == Invoice.Status.SENDING:
            outcome = provider.issue(account.credentials, Draft.from_json(invoice.draft))
        else:
            outcome = provider.status(account.credentials, invoice.reference)
    except (InvalidDocument, NumberTaken) as exc:
        invoice.status = Invoice.Status.REJECTED
        invoice.message = str(exc)
    except (Unavailable, Rejected) as exc:
        invoice.message = str(exc)
    else:
        _apply(invoice, outcome)
    invoice.save()
    return invoice


def refresh_unsettled():
    """The sweep's step: every recent invoice still waiting on someone."""
    for invoice in Invoice.objects.filter(
        status__in=(Invoice.Status.SENDING, Invoice.Status.PENDING),
        created_at__gte=timezone.now() - FOLLOW_FOR,
    ).select_related('tenant'):
        refresh(invoice)


def cancel_for_void(entry, user, reason):
    """
    Called before a payment is voided, inside the same transaction. Money and
    invoice never disagree: if the invoice cannot be cancelled, the payment is
    not voided either.
    """
    invoice = Invoice.objects.select_for_update().filter(cash_entry=entry).first()
    if invoice is None or invoice.status in (Invoice.Status.REJECTED, Invoice.Status.CANCELLED):
        return
    if invoice.status != Invoice.Status.APPROVED:
        raise Refused(
            'The invoice is still waiting on the SET; void the payment once it is approved.',
            'invoice_pending', status.HTTP_409_CONFLICT,
        )
    if timezone.now() - invoice.approved_at > CANCEL_WINDOW:
        raise Refused(
            'The invoice is past the 48 hours in which it can be cancelled; it needs a credit note.',
            'invoice_cancel_window_closed', status.HTTP_409_CONFLICT,
        )
    account = InvoicingAccount.objects.get(tenant=invoice.tenant)
    try:
        get_provider(invoice.provider).cancel(account.credentials, invoice.reference, reason, _actor(user))
    except Unavailable as exc:
        raise Refused('The invoicing provider is not answering; try again shortly.', 'provider_unavailable',
                      status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    except (Rejected, InvalidDocument) as exc:
        raise Refused(str(exc), 'invoice_cancel_rejected', status.HTTP_409_CONFLICT) from exc
    invoice.status = Invoice.Status.CANCELLED
    invoice.cancelled_at = timezone.now()
    invoice.save(update_fields=['status', 'cancelled_at', 'updated_at'])
