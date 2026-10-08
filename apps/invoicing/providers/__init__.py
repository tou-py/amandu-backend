"""
The seam between Kyo and whoever actually talks to SIFEN.

Everything above this package -- models, service, views -- speaks only the
types below. Everything a provider needs to know about its own JSON, codes,
endpoints and errors lives in ONE module next to this file. That is the whole
point: the first provider is new and may not last, and replacing it must be
"write providers/<name>.py, add it to PROVIDERS", not a hunt through the app.

A provider answers in these terms and raises only these exceptions. Anything
else escaping an adapter (a KeyError on a reshaped response, say) is a bug in
the adapter and is allowed to be a 500.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Customer:
    # A RUC with its check digit ("80012345-6"), a cédula ("1234567") or empty.
    # Empty is the anonymous end consumer, which SIFEN allows for B2C.
    tax_id: str
    name: str
    email: str = ''


@dataclass(frozen=True)
class Line:
    description: str
    quantity: int
    # Whole guaraníes, tax INCLUDED -- the price the client was charged.
    unit_price: int
    vat_rate: int  # 0, 5 or 10


@dataclass(frozen=True)
class Draft:
    """
    One invoice as Kyo means it, before any provider has seen it. Stored on the
    Invoice as JSON, so a retry re-sends exactly what was sent the first time.
    """

    establishment: str
    point: str
    number: int
    issued_at: datetime  # aware
    customer: Customer
    lines: tuple[Line, ...]
    # CashEntry.PaymentMethod's values: cash, card, transfer, other.
    payment_method: str
    # Who is issuing it, for the provider's audit trail.
    issued_by_id: str
    issued_by_name: str

    @property
    def total(self):
        return sum(line.quantity * line.unit_price for line in self.lines)

    def to_json(self):
        data = asdict(self)
        data['issued_at'] = self.issued_at.isoformat()
        return data

    @classmethod
    def from_json(cls, data):
        return cls(**{
            **data,
            'issued_at': datetime.fromisoformat(data['issued_at']),
            'customer': Customer(**data['customer']),
            'lines': tuple(Line(**line) for line in data['lines']),
        })


@dataclass(frozen=True)
class Outcome:
    """Where a document stands at the provider. `status` is an Invoice.Status value."""

    reference: str  # the provider's handle on it (the CDC for SIFEN)
    status: str
    message: str = ''
    approved_at: datetime | None = None


class InvalidDocument(Exception):
    """The provider refused the content. Nothing was issued, so the number is free."""

    def __init__(self, errors):
        super().__init__('; '.join(errors))
        self.errors = list(errors)


class NumberTaken(Exception):
    """
    The number is already spent at the provider on a different document --
    typically an account whose next number was set behind what it already
    issued elsewhere. Nothing was issued; moving the next number on fixes it.
    """


class Unavailable(Exception):
    """
    No answer, or an answer that says "later" (timeout, 5xx, 429). The document
    MAY have been issued, so whoever called must keep the number and retry.
    """


class Rejected(Exception):
    """A definite no that is not about the content: a bad key, a suspended account."""


class Provider(Protocol):
    def issue(self, credentials: dict, draft: Draft) -> Outcome: ...

    def status(self, credentials: dict, reference: str) -> Outcome: ...

    def cancel(self, credentials: dict, reference: str, reason: str, actor: tuple[str, str]) -> Outcome: ...

    def pdf(self, credentials: dict, reference: str, layout: str) -> bytes: ...

    def verify(self, credentials: dict) -> None: ...


def _registry():
    # Imported here so a provider module can import the types above.
    from apps.invoicing.providers import sifensis

    return {'sifensis': sifensis.Sifensis()}


# Adding a provider: a module in this package and one entry here. Tests swap
# entries in this dict for a fake.
PROVIDERS: dict[str, Provider] = _registry()


def get_provider(name) -> Provider:
    return PROVIDERS[name]
