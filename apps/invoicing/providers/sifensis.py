"""
SifenSIS (Guaratec): https://docs.sifensis.guaratec.com/ and its /openapi.yaml.

The only module that knows this provider exists. What it does for us: builds
the SIFEN XML from a JSON, signs it with the business's certificate, sends it to
the SET asynchronously and renders the KuDE. The issuer's data (RUC, timbrado,
establishments, CSC) is configured once in their panel per business, and the API
key both identifies that business and picks the environment (SANDBOX,
HOMOLOGACION, PRODUCCION) -- so there is no environment switch on our side.

Every code below was confirmed against a SANDBOX key on 2026-10-07: the
anonymous consumer, cédula and RUC receivers, each payment method, a duplicate
resend, the three KuDE layouts and a cancellation.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

from apps.invoicing.providers import (
    Draft,
    InvalidDocument,
    NumberTaken,
    Outcome,
    Rejected,
    Unavailable,
)

# SIFEN dates are Paraguayan wall-clock time with no offset.
ZONE = ZoneInfo('America/Asuncion')
TIMEOUT = 15  # seconds; signing + queueing on their side, not the SET round trip

STATUS = {
    'GENERADO': 'pending',
    'ENVIADO': 'pending',
    'APROBADO': 'approved',
    'RECHAZADO': 'rejected',
    'CANCELADO': 'cancelled',
}

# CashEntry.PaymentMethod -> catalogue condicionesTiposPagos. Card goes as
# "other" on purpose: 3/4 (credit/debit card) make infoTarjeta.tipo (Visa,
# Mastercard...) mandatory, which the cash book does not record.
PAYMENT = {'cash': 1, 'transfer': 5}

RUC = re.compile(r'^\d+-\d$')


class Sifensis:
    def issue(self, credentials, draft: Draft) -> Outcome:
        body = self._call(credentials, 'POST', '/emitir', json={
            'data': self.document(draft),
            'metadata': self.metadata(draft.issued_by_id, draft.issued_by_name),
        })
        return self._outcome(body)

    def status(self, credentials, reference) -> Outcome:
        return self._outcome(self._call(credentials, 'GET', f'/{reference}/estado'))

    def cancel(self, credentials, reference, reason, actor) -> Outcome:
        body = self._call(credentials, 'POST', '/evento/cancelacion', json={
            'cdc': reference, 'motivo': reason, 'metadata': self.metadata(*actor),
        })
        # The event answers APROBADO or RECHAZADO about the EVENT; an approved
        # cancellation is a cancelled document.
        if body.get('estado') != 'APROBADO':
            raise Rejected(self._sifen_message(body) or 'SIFEN no aprobó la cancelación.')
        return Outcome(reference=reference, status='cancelled')

    def pdf(self, credentials, reference, layout) -> bytes:
        params = {} if layout == 'a4' else {'formato': layout}
        return self._call(credentials, 'GET', f'/{reference}/kude', params=params, raw=True)

    def verify(self, credentials):
        self._call(credentials, 'GET', '/documentos', params={'size': 1})

    # -- mapping ---------------------------------------------------------------

    def document(self, draft: Draft):
        return {
            'tipoDocumento': 1,  # factura
            'establecimiento': draft.establishment,
            'punto': draft.point,
            'numero': draft.number,
            'fecha': draft.issued_at.astimezone(ZONE).strftime('%Y-%m-%dT%H:%M:%S'),
            'tipoEmision': 1,  # normal
            'tipoTransaccion': 2,  # prestación de servicios
            'tipoImpuesto': 1,  # IVA
            'moneda': 'PYG',
            'factura': {'presencia': 1},  # presencial
            'cliente': self.customer(draft.customer),
            'items': [
                {
                    'codigo': str(index),
                    'descripcion': line.description,
                    'unidadMedida': 77,  # unidad
                    'cantidad': line.quantity,
                    'precioUnitario': line.unit_price,
                    'ivaTipo': 1 if line.vat_rate else 3,  # gravado / exento
                    'ivaBase': 100 if line.vat_rate else 0,
                    'iva': line.vat_rate,
                }
                for index, line in enumerate(draft.lines, start=1)
            ],
            'condicion': {
                'tipo': 1,  # contado
                'entregas': [self.payment(draft.payment_method, draft.total)],
            },
        }

    @staticmethod
    def payment(method, amount):
        code = PAYMENT.get(method)
        if code is None:
            return {'tipo': 99, 'tipoDescripcion': 'Tarjeta' if method == 'card' else 'Otro',
                    'monto': amount, 'moneda': 'PYG'}
        return {'tipo': code, 'monto': amount, 'moneda': 'PYG'}

    @staticmethod
    def customer(customer):
        tax_id = customer.tax_id.strip()
        if RUC.match(tax_id):
            return {
                'contribuyente': True,
                'ruc': tax_id,
                # ponytail: Paraguayan legal entities get RUCs starting at 80; a
                # person's RUC is their cédula. Ask /consulta/ruc if this guess
                # ever turns out wrong.
                'tipoContribuyente': 2 if tax_id.startswith('80') else 1,
                'razonSocial': customer.name,
                'tipoOperacion': 1,  # B2B
                'pais': 'PRY',
                **({'email': customer.email} if customer.email else {}),
            }
        if tax_id:
            return {
                'contribuyente': False,
                'documentoTipo': 1,  # cédula paraguaya
                'documentoNumero': tax_id,
                'razonSocial': customer.name,
                'tipoOperacion': 2,  # B2C
                'pais': 'PRY',
                **({'email': customer.email} if customer.email else {}),
            }
        # Anonymous end consumer.
        return {
            'contribuyente': False,
            'documentoTipo': 5,  # innominado
            'documentoNumero': '0',
            'razonSocial': 'Sin Nombre',
            'tipoOperacion': 2,
            'pais': 'PRY',
        }

    @staticmethod
    def metadata(user_id, user_name):
        return {
            'deviceId': 'kyo-api',
            'deviceName': 'Kyo API',
            'userId': str(user_id),
            'userName': user_name,
            'app': 'kyo',
            'version': '1',
        }

    # -- transport -------------------------------------------------------------

    def _call(self, credentials, method, path, raw=False, **kwargs):
        try:
            response = requests.request(
                method,
                settings.SIFENSIS_BASE_URL.rstrip('/') + '/api/v1/de' + path,
                headers={'X-API-Key': credentials['api_key']},
                timeout=TIMEOUT,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise Unavailable(str(exc)) from exc

        code = response.status_code
        if code == 400:
            body = self._json(response)
            raise InvalidDocument(body.get('errores') or [body.get('message') or 'Documento inválido'])
        if code in (401, 403):
            raise Rejected(self._json(response).get('error') or 'SifenSIS rechazó la credencial.')
        if code == 409:
            # Same establishment, point and number, different content.
            raise NumberTaken(self._json(response).get('message') or 'Ese número ya fue usado.')
        if code == 404:
            raise Rejected('SifenSIS no encuentra ese documento.')
        if code == 429 or code >= 500:
            raise Unavailable(f'SifenSIS respondió {code}')
        if code >= 300:
            raise Rejected(f'SifenSIS respondió {code}')
        return response.content if raw else self._json(response)

    @staticmethod
    def _json(response):
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    def _outcome(self, body):
        approved = body.get('fechaAprobacion')
        return Outcome(
            reference=body['cdc'],
            status=STATUS[body['estado']],
            message=body.get('mensajeRespuesta') or self._sifen_message(body),
            approved_at=approved and self._local(approved),
        )

    @staticmethod
    def _sifen_message(body):
        return (body.get('sifenRespuesta') or {}).get('dMsgRes', '')

    @staticmethod
    def _local(value):
        moment = datetime.fromisoformat(value)
        return moment if moment.tzinfo else moment.replace(tzinfo=ZONE)
