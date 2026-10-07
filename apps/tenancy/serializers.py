import re

from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from apps.tenancy.models import Tenant


class TenantSerializer(serializers.ModelSerializer):
    """
    The business as its owner may edit it.

    Three fields are writable and the rest are deliberately not:

      - `slug` is the only globally unique value in the project. It names the
        tenant, travels in URLs and is what a support conversation refers to.
        Renaming it is a migration with redirects, not a profile edit.
      - `status` decides whether everyone inside can work at all. Suspending or
        closing a business is a platform action with billing behind it; a
        settings form is not where that should be one mis-click away.
      - `id` and the timestamps are facts, not preferences.

    `timezone` and `country` ARE writable, and both carry weight: the timezone
    is how every stored instant becomes a time on the agenda, and the country is
    the region local phone numbers are parsed against. Changing either does not
    move existing data -- the appointments keep their instants, the numbers keep
    their stored E.164 form -- it changes how the next ones are read.

    So is `public_booking`, which decides whether the business has a public
    booking page at all. Opening one and closing it again are the owner's calls
    to make from a settings screen; the alternative is a support request for a
    checkbox. It defaults to off for the reason on the model field.

    `phone` is the business's own number. Setting it is what turns WhatsApp
    notices on for this business: every message gives it as the contact, and a
    message nobody can answer is worse than none (apps/scheduling/whatsapp.py).

    And so are `plan_period_start` and `plan_grace_days`, how this business
    collects its monthly plans. Changing them prefills the NEXT subscription
    and moves the due dates read from now on; no subscription's anchor moves.
    """

    # Declared rather than inferred so the model's `^[A-Z]{2}$` validator does
    # not run first: DRF applies a field's validators BEFORE validate_<field>,
    # so a lower-case code would be rejected before it could be normalised. The
    # same country written two ways is the same country, exactly as it is for
    # the phone numbers this field governs.
    country = serializers.CharField(max_length=2, allow_blank=True, required=False)
    phone = PhoneNumberField(required=False, allow_blank=True)

    class Meta:
        model = Tenant
        fields = (
            'id', 'name', 'slug', 'status', 'timezone', 'country',
            'public_booking', 'phone', 'plan_period_start', 'plan_grace_days', 'created_at',
        )
        read_only_fields = ('id', 'slug', 'status', 'created_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Read in the business's own country, like its clients' numbers.
        # The stored one: a country changed in this same PATCH applies to
        # numbers typed from the next request on.
        if isinstance(self.instance, Tenant) and self.instance.country:
            self.fields['phone'].region = self.instance.country

    def validate_name(self, value):
        # A business with a blank name would render as an empty header and an
        # empty tab title, with nothing on screen saying why.
        if not value.strip():
            raise serializers.ValidationError('The business needs a name.')
        return value.strip()

    def validate_country(self, value):
        code = value.strip().upper()
        # Blank is a defined state, not a hole: it means local numbers are not
        # parsed at all and must arrive already international.
        if code and not re.fullmatch(r'[A-Z]{2}', code):
            raise serializers.ValidationError('Use an ISO 3166-1 alpha-2 code.')
        return code
