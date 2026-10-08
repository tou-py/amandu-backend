from datetime import date, timedelta

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership
from apps.invoicing import providers
from apps.invoicing.models import Invoice, InvoicingAccount
from apps.invoicing.providers import (
    InvalidDocument,
    NumberTaken,
    Outcome,
    Rejected,
    Unavailable,
)
from apps.scheduling.models import Client
from apps.tenancy.models import Tenant

LIST_URL = reverse('invoicing:invoice-list')
ACCOUNT_URL = reverse('invoicing:account')


def api(user, tenant):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


class FakeProvider:
    """Stands in for a real provider in PROVIDERS. Queue an exception in `fail`
    to make the next call raise it."""

    def __init__(self):
        self.issued, self.cancelled, self.fail = [], [], []
        self.status_answer = 'pending'

    def _maybe_fail(self):
        if self.fail:
            raise self.fail.pop(0)

    def issue(self, credentials, draft):
        self._maybe_fail()
        self.issued.append((credentials, draft))
        return Outcome(reference=f'CDC{draft.number:041d}', status='pending')

    def status(self, credentials, reference):
        self._maybe_fail()
        return Outcome(reference=reference, status=self.status_answer, message='Aprobado')

    def cancel(self, credentials, reference, reason, actor):
        self._maybe_fail()
        self.cancelled.append((reference, reason))
        return Outcome(reference=reference, status='cancelled')

    def pdf(self, credentials, reference, layout):
        self._maybe_fail()
        return b'%PDF-' + layout.encode()

    def verify(self, credentials):
        self._maybe_fail()


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setitem(providers.PROVIDERS, 'sifensis', provider)
    return provider


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY')


def member(django_user_model, tenant, role, email):
    user = django_user_model.objects.create_user(email=email, password='pw', first_name='Ana')
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return user


@pytest.fixture
def owner(db, django_user_model, salon):
    return member(django_user_model, salon, Membership.Role.OWNER, 'o@example.com')


@pytest.fixture
def desk(db, django_user_model, salon):
    return member(django_user_model, salon, Membership.Role.COORDINATOR, 'c@example.com')


@pytest.fixture
def stylist(db, django_user_model, salon):
    return member(django_user_model, salon, Membership.Role.STAFF, 's@example.com')


@pytest.fixture
def account(salon):
    account = InvoicingAccount(tenant=salon, provider='sifensis')
    account.credentials = {'api_key': 'secret'}
    account.save()
    return account


@pytest.fixture
def client_file(salon):
    return Client.objects.create(tenant=salon, name='Lucía', phone='+595981111111', email='l@example.com')


def income(salon, client=None, amount=80_000, **extra):
    return CashEntry.objects.create(
        tenant=salon, kind=CashEntry.Kind.INCOME, amount=amount, occurred_on=date(2026, 10, 7),
        concept='Turno · Corte · Lucía', payment_method='cash', client=client, **extra,
    )


# -- issuing -----------------------------------------------------------------


def test_invoicing_a_payment_spends_the_next_number(fake, account, desk, salon, client_file):
    first = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon, client_file).pk})
    second = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk})

    assert first.status_code == 201, first.data
    assert first.data['status'] == 'pending'
    assert first.data['display_number'] == '001-001-0000001'
    assert second.data['number'] == 2
    account.refresh_from_db()
    assert account.next_number == 3
    credentials, draft = fake.issued[0]
    assert credentials == {'api_key': 'secret'}
    assert draft.total == 80_000
    assert draft.lines[0].vat_rate == 10
    assert draft.issued_by_name == 'Ana'


def test_without_a_tax_id_it_goes_to_the_anonymous_end_consumer(fake, account, desk, salon, client_file):
    api(desk, salon).post(LIST_URL, {'cash_entry': income(salon, client_file).pk})

    assert fake.issued[0][1].customer.tax_id == ''


def test_the_client_file_tax_id_is_used(fake, account, desk, salon, client_file):
    client_file.tax_id, client_file.tax_name = '80012345-6', 'Lucía S.A.'
    client_file.save()

    response = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon, client_file).pk})

    customer = fake.issued[0][1].customer
    assert (customer.tax_id, customer.name) == ('80012345-6', 'Lucía S.A.')
    assert response.data['customer_name'] == 'Lucía S.A.'


def test_an_override_can_be_saved_to_the_client_file(fake, account, desk, salon, client_file):
    api(desk, salon).post(LIST_URL, {
        'cash_entry': income(salon, client_file).pk,
        'tax_id': '1234567', 'tax_name': 'Lucía Benítez', 'save_to_client': True,
    })

    client_file.refresh_from_db()
    assert (client_file.tax_id, client_file.tax_name) == ('1234567', 'Lucía Benítez')
    assert fake.issued[0][1].customer.tax_id == '1234567'


def test_a_refused_document_does_not_spend_the_number(fake, account, desk, salon):
    fake.fail.append(InvalidDocument(['Debe especificar los items']))

    response = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk})

    assert response.status_code == 400
    assert response.data['code'] == 'invalid_invoice'
    assert response.data['errors'] == ['Debe especificar los items']
    assert not Invoice.objects.exists()
    account.refresh_from_db()
    assert account.next_number == 1


def test_a_number_already_used_at_the_provider_says_how_to_fix_it(fake, account, desk, salon):
    fake.fail.append(NumberTaken('Ya existe un documento vigente con la numeración 001-001-0000001'))

    response = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk})

    assert response.status_code == 409
    assert response.data['code'] == 'invoice_number_taken'
    assert not Invoice.objects.exists()


def test_no_answer_keeps_the_number_and_the_sweep_resends_the_same_draft(fake, account, desk, salon, settings):
    settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT = 'k', 'mailto:ops@example.com'
    fake.fail.append(Unavailable('timeout'))

    response = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk})
    assert response.status_code == 201
    assert response.data['status'] == 'sending'

    call_command('send_reminders')

    invoice = Invoice.objects.get()
    assert invoice.status == 'pending'
    assert invoice.number == 1
    assert fake.issued[0][1].number == 1


def test_the_sweep_follows_a_pending_invoice_to_approval(fake, account, desk, salon, settings):
    settings.VAPID_PRIVATE_KEY, settings.VAPID_SUBJECT = 'k', 'mailto:ops@example.com'
    api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk})
    fake.status_answer = 'approved'

    call_command('send_reminders')

    invoice = Invoice.objects.get()
    assert invoice.status == 'approved'
    assert invoice.approved_at is not None


def test_refresh_asks_now(fake, account, desk, salon):
    invoice_id = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk}).data['id']
    fake.status_answer = 'rejected'

    response = api(desk, salon).post(reverse('invoicing:invoice-refresh', args=[invoice_id]))

    assert response.data['status'] == 'rejected'
    assert response.data['message'] == 'Aprobado'


def test_a_rejected_invoice_is_issued_again_under_its_own_number(fake, account, desk, salon):
    entry = income(salon)
    first = api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk}).data
    Invoice.objects.filter(pk=first['id']).update(status='rejected', message='RUC inexistente')

    again = api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk, 'tax_id': '', 'tax_name': ''})

    assert again.status_code == 201, again.data
    assert (again.data['id'], again.data['number'], again.data['status']) == (first['id'], 1, 'pending')
    assert again.data['message'] == ''
    assert [draft.number for _, draft in fake.issued] == [1, 1]
    account.refresh_from_db()
    assert account.next_number == 2


def test_a_payment_is_invoiced_once(fake, account, desk, salon):
    entry = income(salon)
    api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk})

    response = api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk})

    assert response.status_code == 409
    assert response.data['code'] == 'already_invoiced'


@pytest.mark.parametrize('entry_kwargs, code', [
    ({'kind': CashEntry.Kind.EXPENSE}, 'not_income'),
    ({'voided_at': timezone.now()}, 'voided'),
])
def test_only_a_live_income_can_be_invoiced(fake, account, desk, salon, entry_kwargs, code):
    entry = income(salon)
    CashEntry.objects.filter(pk=entry.pk).update(**entry_kwargs)

    response = api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk})

    assert response.status_code == 400
    assert response.data['code'] == code


def test_without_an_account_it_says_so(fake, desk, salon):
    response = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk})

    assert response.status_code == 409
    assert response.data['code'] == 'invoicing_not_configured'


def test_another_shops_payment_is_not_found(fake, account, desk, salon):
    other = Tenant.objects.create(name='Other', slug='other', country='PY')

    response = api(desk, salon).post(LIST_URL, {'cash_entry': income(other).pk})

    assert response.status_code == 400


def test_staff_cannot_invoice(fake, account, stylist, salon):
    response = api(stylist, salon).post(LIST_URL, {'cash_entry': income(salon).pk})

    assert response.status_code == 403


def test_the_list_filters_by_payment(fake, account, desk, salon):
    entry = income(salon)
    api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk})
    api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk})

    response = api(desk, salon).get(LIST_URL, {'cash_entry': entry.pk})

    assert [row['cash_entry'] for row in response.data['results']] == [entry.pk]


def test_the_pdf_is_proxied(fake, account, desk, salon):
    invoice_id = api(desk, salon).post(LIST_URL, {'cash_entry': income(salon).pk}).data['id']

    response = api(desk, salon).get(reverse('invoicing:invoice-pdf', args=[invoice_id]), {'layout': 'ticket'})

    assert response.status_code == 200
    assert response['Content-Type'] == 'application/pdf'
    assert response.content == b'%PDF-ticket'


# -- voiding -----------------------------------------------------------------


def void(user, salon, entry):
    return api(user, salon).post(
        reverse('accounting:cashentry-void', args=[entry.pk]), {'reason': 'cobrado de más'},
    )


def approved(salon, hours_ago=1):
    entry = income(salon)
    Invoice.objects.create(
        tenant=salon, cash_entry=entry, provider='sifensis', establishment='001', point='001',
        number=1, draft={}, total=entry.amount, customer_name='Sin Nombre', status='approved',
        reference='CDC1', approved_at=timezone.now() - timedelta(hours=hours_ago),
    )
    return entry


def test_voiding_an_invoiced_payment_cancels_the_invoice(fake, account, owner, salon):
    entry = approved(salon)

    response = void(owner, salon, entry)

    assert response.status_code == 200
    assert fake.cancelled == [('CDC1', 'cobrado de más')]
    assert Invoice.objects.get().status == 'cancelled'


def test_past_the_window_the_payment_stays(fake, account, owner, salon):
    entry = approved(salon, hours_ago=49)

    response = void(owner, salon, entry)

    assert response.status_code == 409
    assert response.data['code'] == 'invoice_cancel_window_closed'
    entry.refresh_from_db()
    assert entry.voided_at is None


def test_a_pending_invoice_blocks_the_void(fake, account, owner, desk, salon):
    entry = income(salon)
    api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk})

    response = void(owner, salon, entry)

    assert response.data['code'] == 'invoice_pending'


@pytest.mark.parametrize('error, code, http', [
    (Unavailable('down'), 'provider_unavailable', 503),
    (Rejected('no'), 'invoice_cancel_rejected', 409),
])
def test_a_failed_cancellation_keeps_the_payment_alive(fake, account, owner, salon, error, code, http):
    entry = approved(salon)
    fake.fail.append(error)

    response = void(owner, salon, entry)

    assert (response.status_code, response.data['code']) == (http, code)
    entry.refresh_from_db()
    assert entry.voided_at is None
    assert Invoice.objects.get().status == 'approved'


def test_a_payment_without_invoice_voids_as_before(fake, owner, salon):
    response = void(owner, salon, income(salon))

    assert response.status_code == 200


# -- account -----------------------------------------------------------------


def test_the_account_reads_as_defaults_before_it_exists(fake, owner, salon):
    response = api(owner, salon).get(ACCOUNT_URL)

    assert response.data == {
        'provider': 'sifensis', 'has_credentials': False, 'establishment': '001', 'point': '001',
        'next_number': 1, 'active': True,
    }


def test_the_key_is_verified_stored_encrypted_and_never_returned(fake, owner, salon):
    response = api(owner, salon).put(ACCOUNT_URL, {
        'provider': 'sifensis', 'api_key': 'k-123', 'establishment': '002', 'point': '001', 'next_number': 40,
    })

    assert response.status_code == 200, response.data
    assert 'api_key' not in response.data
    assert response.data['has_credentials'] is True
    account = InvoicingAccount.objects.get(tenant=salon)
    assert 'k-123' not in account.credentials_encrypted
    assert account.credentials == {'api_key': 'k-123'}
    assert (account.establishment, account.next_number) == ('002', 40)


def test_a_key_the_provider_refuses_is_a_400(fake, owner, salon):
    fake.fail.append(Rejected('API key inválida'))

    response = api(owner, salon).put(ACCOUNT_URL, {'provider': 'sifensis', 'api_key': 'bad'})

    assert response.status_code == 400
    assert response.data['code'] == 'invalid_credentials'
    assert not InvoicingAccount.objects.exists()


def test_an_unknown_provider_is_refused(fake, owner, salon):
    response = api(owner, salon).put(ACCOUNT_URL, {'provider': 'nope', 'api_key': 'k'})

    assert response.status_code == 400


def test_the_front_desk_cannot_touch_the_account(fake, desk, salon):
    assert api(desk, salon).get(ACCOUNT_URL).status_code == 403


# -- what the front reads ----------------------------------------------------


def test_a_payment_carries_its_invoice(fake, account, desk, salon, client_file):
    entry = income(salon, client_file)
    income(salon, client_file)
    api(desk, salon).post(LIST_URL, {'cash_entry': entry.pk})

    response = api(desk, salon).get(reverse('scheduling:client-payments', args=[client_file.pk]))

    invoices = {row['id']: row['invoice'] for row in response.data['results']}
    assert invoices[entry.pk]['display_number'] == '001-001-0000001'
    assert invoices[entry.pk]['status'] == 'pending'
    assert list(invoices.values()).count(None) == 1


def test_the_session_says_whether_the_business_invoices(fake, desk, salon):
    me = reverse('accounts:me')
    assert api(desk, salon).get(me).data['memberships'][0]['tenant_invoicing'] is False

    account = InvoicingAccount(tenant=salon, provider='sifensis')
    account.credentials = {'api_key': 'k'}
    account.save()

    assert api(desk, salon).get(me).data['memberships'][0]['tenant_invoicing'] is True


def test_the_work_list_holds_what_still_needs_an_invoice(fake, account, desk, salon, client_file):
    done = income(salon, client_file)
    waiting = income(salon, client_file, amount=50_000)
    refused = income(salon)
    old = income(salon)
    expense = income(salon)
    today = timezone.localdate()
    CashEntry.objects.filter(pk__in=[done.pk, waiting.pk, refused.pk, expense.pk]).update(occurred_on=today)
    CashEntry.objects.filter(pk=old.pk).update(occurred_on=today - timedelta(days=40))
    CashEntry.objects.filter(pk=expense.pk).update(kind=CashEntry.Kind.EXPENSE)
    api(desk, salon).post(LIST_URL, {'cash_entry': done.pk})
    rejected_id = api(desk, salon).post(LIST_URL, {'cash_entry': refused.pk}).data['id']
    Invoice.objects.filter(pk=rejected_id).update(status='rejected', message='RUC inexistente')

    rows = api(desk, salon).get(reverse('invoicing:invoice-to-issue')).data

    assert {row['id']: row['rejected'] for row in rows} == {waiting.pk: None, refused.pk: 'RUC inexistente'}
    assert next(row for row in rows if row['id'] == waiting.pk)['client']['name'] == 'Lucía'


def test_an_invoice_reads_with_its_payment(fake, account, desk, salon, client_file):
    api(desk, salon).post(LIST_URL, {'cash_entry': income(salon, client_file).pk})

    row = api(desk, salon).get(LIST_URL).data['results'][0]

    assert (row['concept'], row['client']['name']) == ('Turno · Corte · Lucía', 'Lucía')
