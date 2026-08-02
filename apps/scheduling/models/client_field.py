from datetime import date

from django.core.exceptions import ValidationError
from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class ClientField(TenantOwnedMixin, TimestampMixin):
    """
    One extra question this tenant asks about every client, defined by the tenant
    itself: the dentist's medical history, the salon's colour formula, the pilates
    studio's injuries.

    It is the SAME abstraction for all three, which is the whole point -- a model
    per vertical would be branching business logic by industry, which the product
    forbids. What differs between a dental practice and a salon is which rows live
    in this table, and that is data, not code.

    The answers do not live here. They live in `Client.custom_data`, a jsonb column
    keyed by `key`, because the alternative -- a value row per field per client --
    turns reading one client's file into a join and a fan-out for something that is
    always read whole and never queried across clients. This table is the schema;
    the client row is the record.

    Consequence, and the reason the serializer does the work: the database cannot
    type-check a value it stores as JSON. `clean_value` below is the only guard,
    the same trade already made for Appointment.capacity.
    """

    class Kind(models.TextChoices):
        TEXT = 'text', 'Text'
        NUMBER = 'number', 'Number'
        DATE = 'date', 'Date'
        # Named BOOLEAN and not CHECKBOX: a checkbox is how a browser draws it,
        # and this table says what the value IS, not which widget renders it.
        BOOLEAN = 'boolean', 'Yes/no'
        SELECT = 'select', 'Choice'

    # The key inside custom_data, chosen once and then load-bearing: every client
    # answer is filed under it. A slug and not free text because it travels as a
    # JSON key. Renaming it would orphan every stored answer, so the serializer
    # refuses to change it after creation -- a rename is a data migration, not an
    # edit. `label` is what changes when the wording changes.
    key = models.SlugField(max_length=40)
    label = models.CharField(max_length=120)
    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,  # type: ignore
        default=Kind.TEXT,
    )
    # Only meaningful for SELECT: the allowed answers, in the order they are
    # offered. A plain list of strings -- a value/label pair would be a second
    # vocabulary to keep in sync for a form that shows the value anyway.
    options = models.JSONField(default=list, blank=True)
    required = models.BooleanField(default=False)
    # Where it sits in the form. Ties break by id, so fields added later without a
    # position land after the ones already there instead of shuffling every render.
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = 'tb_client_field'
        ordering = ('position', 'id')
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'key'],
                name='unique_client_field_key_per_tenant',
            ),
        ]

    def __str__(self):
        return self.label

    def clean_value(self, value):
        """
        Whether `value` is a legal answer to this question, and what to store for it.

        JSON has no date and no decimal, so DATE round-trips as an ISO string:
        parsing it here is what stops '2026-31-31' from being filed as an answer
        and blowing up whoever reads the file next.

        Returns the value to store. Raises ValidationError, deliberately Django's
        and not DRF's, so the rule can be reused off the API -- a seed command or
        an import -- without dragging a request in.
        """
        if self.kind == self.Kind.TEXT:
            if not isinstance(value, str):
                raise ValidationError('Expected text.')
            return value.strip()

        if self.kind == self.Kind.NUMBER:
            # bool is a subclass of int in Python, so True would pass an
            # isinstance(value, int) check and be stored as the number 1.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError('Expected a number.')
            return value

        if self.kind == self.Kind.BOOLEAN:
            if not isinstance(value, bool):
                raise ValidationError('Expected true or false.')
            return value

        if self.kind == self.Kind.DATE:
            try:
                return date.fromisoformat(value).isoformat()
            except (TypeError, ValueError):
                raise ValidationError('Expected a date as YYYY-MM-DD.')

        if value not in self.options:
            raise ValidationError(f'Expected one of {self.options}.')
        return value
