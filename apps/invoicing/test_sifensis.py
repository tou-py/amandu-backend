from datetime import UTC, datetime

import pytest
import requests

from apps.invoicing.providers import (
    Customer,
    Draft,
    InvalidDocument,
    Line,
    NumberTaken,
    Rejected,
    Unavailable,
)
from apps.invoicing.providers.sifensis import Sifensis

KEY = {'api_key': 'k'}


def draft(tax_id='', name='', method='cash'):
    return Draft(
        establishment='001', point='002', number=7,
        issued_at=datetime(2026, 10, 7, 14, 30, tzinfo=UTC),
        customer=Customer(tax_id=tax_id, name=name),
        lines=(Line('Corte', 1, 80_000, 10),), payment_method=method,
        issued_by_id='3', issued_by_name='Ana',
    )


class Answer:
    def __init__(self, status, body=None, content=b''):
        self.status_code, self.body, self.content = status, body, content

    def json(self):
        if self.body is None:
            raise ValueError
        return self.body


@pytest.fixture
def http(monkeypatch):
    calls, answers = [], []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(requests, 'request', request)
    return calls, answers


def test_issue_sends_the_document_and_reads_the_cdc(http, settings):
    calls, answers = http
    answers.append(Answer(201, {'cdc': '0' * 44, 'estado': 'GENERADO'}))

    outcome = Sifensis().issue(KEY, draft())

    method, url, kwargs = calls[0]
    assert (method, url) == ('POST', 'https://sifensis.test/api/v1/de/emitir')
    assert kwargs['headers'] == {'X-API-Key': 'k'}
    data = kwargs['json']['data']
    # Asunción wall clock, no offset: 14:30 UTC is 11:30 there in October.
    assert data['fecha'] == '2026-10-07T11:30:00'
    assert (data['establecimiento'], data['punto'], data['numero']) == ('001', '002', 7)
    assert data['items'][0] == {
        'codigo': '1', 'descripcion': 'Corte', 'unidadMedida': 77, 'cantidad': 1,
        'precioUnitario': 80_000, 'ivaTipo': 1, 'ivaBase': 100, 'iva': 10,
    }
    assert data['condicion'] == {'tipo': 1, 'entregas': [{'tipo': 1, 'monto': 80_000, 'moneda': 'PYG'}]}
    assert kwargs['json']['metadata']['userName'] == 'Ana'
    assert (outcome.reference, outcome.status) == ('0' * 44, 'pending')


@pytest.mark.parametrize('tax_id, expected', [
    ('', {'contribuyente': False, 'documentoTipo': 5, 'razonSocial': 'Sin Nombre', 'tipoOperacion': 2}),
    ('1234567', {'contribuyente': False, 'documentoTipo': 1, 'documentoNumero': '1234567', 'tipoOperacion': 2}),
    ('80012345-6', {'contribuyente': True, 'ruc': '80012345-6', 'tipoContribuyente': 2, 'tipoOperacion': 1}),
    ('4567890-1', {'contribuyente': True, 'tipoContribuyente': 1}),
])
def test_the_receiver_follows_the_tax_id(tax_id, expected):
    cliente = Sifensis().document(draft(tax_id, 'Lucía'))['cliente']

    assert cliente | expected == cliente


@pytest.mark.parametrize('method, expected', [
    ('transfer', {'tipo': 5}),
    ('card', {'tipo': 99, 'tipoDescripcion': 'Tarjeta'}),
    ('other', {'tipo': 99, 'tipoDescripcion': 'Otro'}),
])
def test_payment_methods(method, expected):
    entrega = Sifensis().document(draft(method=method))['condicion']['entregas'][0]

    assert entrega | expected == entrega


def test_status_reads_the_approval_in_asuncion_time(http):
    _, answers = http
    answers.append(Answer(200, {
        'cdc': 'C', 'estado': 'APROBADO', 'fechaAprobacion': '2026-10-07T10:05:30', 'mensajeRespuesta': 'Aprobado',
    }))

    outcome = Sifensis().status(KEY, 'C')

    assert outcome.status == 'approved'
    assert outcome.approved_at == datetime(2026, 10, 7, 13, 5, 30, tzinfo=UTC)


@pytest.mark.parametrize('answer, error', [
    (Answer(400, {'errores': ['Falta cliente']}), InvalidDocument),
    (Answer(401, {'error': 'API key inválida'}), Rejected),
    (Answer(409, {'message': 'Ya existe un documento vigente'}), NumberTaken),
    (Answer(429, {}), Unavailable),
    (Answer(503, None), Unavailable),
    (requests.Timeout('slow'), Unavailable),
])
def test_errors_map_to_the_port(http, answer, error):
    http[1].append(answer)

    with pytest.raises(error):
        Sifensis().issue(KEY, draft())


def test_a_refused_cancellation_is_rejected(http):
    http[1].append(Answer(200, {'estado': 'RECHAZADO', 'sifenRespuesta': {'dMsgRes': 'Fuera de plazo'}}))

    with pytest.raises(Rejected, match='Fuera de plazo'):
        Sifensis().cancel(KEY, 'C', 'error', ('3', 'Ana'))


def test_pdf_asks_for_the_layout(http):
    calls, answers = http
    answers.append(Answer(200, content=b'%PDF'))

    assert Sifensis().pdf(KEY, 'C', 'ticket58') == b'%PDF'
    assert calls[0][2]['params'] == {'formato': 'ticket58'}
