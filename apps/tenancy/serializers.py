import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from drf_spectacular.utils import extend_schema_field
from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from apps.commons import images
from apps.tenancy.models import Tenant


class LogoMetaSerializer(serializers.Serializer):
    """What the panel measured of a logo on upload. Cosmetic only: a lie here
    draws the logo on the wrong plate, nothing more, so it is shape-checked
    and taken at its word."""

    aspect = serializers.FloatField(min_value=0.05, max_value=20, help_text='Width over height.')
    transparent = serializers.BooleanField()
    background = serializers.RegexField(
        r'^#[0-9a-fA-F]{6}$', allow_null=True, help_text='Its own background, null when transparent.',
    )
    ink = serializers.ChoiceField(choices=('dark', 'light'), help_text='Whether the drawing itself is dark or light.')
    colors = serializers.ListField(
        child=serializers.RegexField(r'^#[0-9a-fA-F]{6}$'), max_length=4, required=False,
        help_text="Its main colours, most of it first: what the palette editor offers as the shop's colour.",
    )

    def validate_background(self, value):
        return value and value.lower()

    def validate_colors(self, value):
        return [color.lower() for color in value]


@extend_schema_field(LogoMetaSerializer(allow_null=True))
class LogoMetaField(serializers.JSONField):
    def to_internal_value(self, data):
        meta = LogoMetaSerializer(data=data)
        meta.is_valid(raise_exception=True)
        return dict(meta.validated_data)


_SCHEME = re.compile(r'^[a-z][a-z0-9+.-]*://', re.I)
_http_url = URLValidator(schemes=('http', 'https'))


def _as_url(value, message):
    """`value` as an http(s) URL, https:// assumed when the owner left it out,
    which is how a link is usually typed."""
    url = value if _SCHEME.match(value) else f'https://{value}'
    try:
        _http_url(url)
    except DjangoValidationError:
        raise serializers.ValidationError(message)
    return url


# Where each network keeps its profiles, and what a handle on it may be made of.
# Hosts are compared without a leading www. / m.
SOCIAL = {
    'instagram': ('Instagram', {'instagram.com'}, re.compile(r'[A-Za-z0-9._]{1,30}')),
    'facebook': ('Facebook', {'facebook.com', 'fb.com'}, re.compile(r'[A-Za-z0-9.-]{1,60}')),
    'tiktok': ('TikTok', {'tiktok.com'}, re.compile(r'[A-Za-z0-9._]{1,30}')),
}


def social_handle(network, value):
    """
    The bare handle out of whatever the owner pasted: '@salon', 'salon', or the
    link to the profile copied from the app (any scheme, www or not, query and
    trailing slash ignored). Stored bare so the page builds link and label one
    way.

    Anything with a slash, or starting with the network's own host, is read as
    a link: 'salon.lumiere' is a valid Instagram handle, not a domain.
    """
    name, hosts, shape = SOCIAL[network]
    value = value.strip()
    if not value:
        return ''
    bare = _SCHEME.sub('', value)
    if '/' in bare or bare.lower().removeprefix('www.').split('/')[0] in hosts:
        parts = urlsplit(f'https://{bare}')
        host = (parts.hostname or '').removeprefix('www.').removeprefix('m.')
        if host not in hosts:
            raise serializers.ValidationError(f'Ese link no es de {name}. Pegá el de tu perfil o tu @usuario.')
        segment = parts.path.strip('/').split('/')[0]
        # TikTok profiles are /@handle; a bare /handle there is something else.
        if network == 'tiktok' and not segment.startswith('@'):
            segment = ''
        value = segment
    handle = value.removeprefix('@')
    if not shape.fullmatch(handle):
        raise serializers.ValidationError(f'Pegá tu @usuario de {name} o el link a tu perfil.')
    return handle


# google.com, google.com.py, google.co.uk, google.de...
_GOOGLE = re.compile(r'(www\.)?google\.(com|com\.[a-z]{2}|co\.[a-z]{2}|[a-z]{2})')


def is_maps_link(url):
    parts = urlsplit(url)
    host, path = (parts.hostname or ''), parts.path
    if host == 'maps.app.goo.gl':
        return True
    if host == 'goo.gl' or _GOOGLE.fullmatch(host):
        return path.startswith('/maps')
    # maps.google.com?q=... is an older share format with the place in the query.
    return bool(host.startswith('maps.') and _GOOGLE.fullmatch(host.removeprefix('maps.')))


class TenantSerializer(images.ImageKeysMixin, serializers.ModelSerializer):
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

    `phone` is the business's own number. Setting it is what turns WhatsApp
    notices on for this business: every message gives it as the contact, and a
    message nobody can answer is worse than none (apps/scheduling/notify.py).

    So are `request_accepted_message` and `request_rejected_message`, how the
    business answers a public request by WhatsApp. Blank means Kyo's default.

    And so are `plan_period_start` and `plan_grace_days`, how this business
    collects its monthly plans. Changing them prefills the NEXT subscription
    and moves the due dates read from now on; no subscription's anchor moves.

    And the business's place and presence for the public page: `address`,
    `maps_url`, `instagram`, `facebook`, `tiktok` and `website`. The owner
    pastes whatever their phone gives them; it is stored one way (bare handles,
    full https URLs) and refused when it points somewhere else.
    """

    # Declared rather than inferred so the model's `^[A-Z]{2}$` validator does
    # not run first: DRF applies a field's validators BEFORE validate_<field>,
    # so a lower-case code would be rejected before it could be normalised. The
    # same country written two ways is the same country, exactly as it is for
    # the phone numbers this field governs.
    country = serializers.CharField(max_length=2, allow_blank=True, required=False)
    phone = PhoneNumberField(required=False, allow_blank=True)
    logo_key = images.KeyField('logo')
    logo_url = images.UrlField(source='logo_key')
    logo_meta = LogoMetaField(required=False, allow_null=True)
    # Declared for the same reason as `country`: the model's URLValidator would
    # refuse "minegocio.com" before validate_<field> could add the https://.
    maps_url = serializers.CharField(max_length=300, allow_blank=True, required=False)
    website = serializers.CharField(max_length=200, allow_blank=True, required=False)

    class Meta:
        model = Tenant
        fields = (
            'id', 'name', 'slug', 'status', 'timezone', 'country',
            'phone', 'plan_period_start', 'plan_grace_days', 'logo_key', 'logo_url', 'logo_meta',
            'brand_color', 'brand_intensity', 'request_accepted_message', 'request_rejected_message',
            'address', 'maps_url', 'instagram', 'facebook', 'tiktok', 'website', 'created_at',
        )
        read_only_fields = ('id', 'slug', 'status', 'created_at')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Read in the business's own country, like its clients' numbers.
        # The stored one: a country changed in this same PATCH applies to
        # numbers typed from the next request on.
        if isinstance(self.instance, Tenant) and self.instance.country:
            self.fields['phone'].region = self.instance.country

    def to_internal_value(self, data):
        # A colour picker may send #ABCDEF; it is stored the one way.
        if isinstance(data, dict) and isinstance(data.get('brand_color'), str):
            data = {**data, 'brand_color': data['brand_color'].lower()}
        return super().to_internal_value(data)

    def validate_name(self, value):
        # A business with a blank name would render as an empty header and an
        # empty tab title, with nothing on screen saying why.
        if not value.strip():
            raise serializers.ValidationError('The business needs a name.')
        return value.strip()

    def validate(self, attrs):
        # The measurements describe one picture: they arrive with it, and a new
        # logo sent without them (or no logo) leaves none behind.
        if 'logo_key' not in attrs:
            if 'logo_meta' in attrs:
                raise serializers.ValidationError(
                    {'logo_meta': 'Send it with the logo it measures.'}, code='logo_meta_alone',
                )
        elif attrs['logo_key'] is None or (
            'logo_meta' not in attrs and attrs['logo_key'] != getattr(self.instance, 'logo_key', None)
        ):
            attrs['logo_meta'] = None
        return attrs

    def validate_address(self, value):
        return value.strip()

    def validate_maps_url(self, value):
        value = value.strip()
        if not value:
            return ''
        message = 'Pegá el link de Google Maps de tu negocio.'
        url = _as_url(value, message)
        if not is_maps_link(url):
            raise serializers.ValidationError(message)
        return url

    def validate_website(self, value):
        value = value.strip()
        return value and _as_url(value, 'Pegá la dirección de tu página, por ejemplo minegocio.com.')

    def validate_instagram(self, value):
        return social_handle('instagram', value)

    def validate_facebook(self, value):
        return social_handle('facebook', value)

    def validate_tiktok(self, value):
        return social_handle('tiktok', value)

    def validate_country(self, value):
        code = value.strip().upper()
        # Blank is a defined state, not a hole: it means local numbers are not
        # parsed at all and must arrive already international.
        if code and not re.fullmatch(r'[A-Z]{2}', code):
            raise serializers.ValidationError('Use an ISO 3166-1 alpha-2 code.')
        return code


class SubscriptionLineSerializer(serializers.Serializer):
    concept = serializers.ChoiceField(choices=('base', 'extra_professionals', 'cobros', 'caja', 'reservas', 'facturacion'))
    quantity = serializers.IntegerField()
    amount = serializers.IntegerField(help_text='Guaraníes a month.')


class SetupFactsSerializer(serializers.Serializer):
    logo = serializers.BooleanField()
    photo = serializers.BooleanField(help_text="The owner's own photo.")
    hours = serializers.BooleanField()
    services = serializers.BooleanField()
    clients = serializers.BooleanField()
    plans = serializers.BooleanField()
    invited = serializers.BooleanField(help_text='Another member, or an invitation pending.')
    appointments = serializers.BooleanField()


class AddonsSerializer(serializers.Serializer):
    """The add-ons an owner wants on; the ones left out go off."""

    addons = serializers.ListField(child=serializers.ChoiceField(choices=('cobros', 'reservas')), allow_empty=True)


class KyoSubscriptionSerializer(serializers.Serializer):
    """What this business pays Kyo, read-only: everything here is changed by
    Kyo in the admin, on a transfer or a conversation."""

    plan = serializers.ChoiceField(choices=('solo', 'equipo'))
    professionals = serializers.ListField(
        child=serializers.CharField(),
        help_text='Names of the members who attend appointments: who the plan counts.',
    )
    addons = serializers.ListField(child=serializers.ChoiceField(choices=('cobros', 'caja', 'reservas', 'facturacion')))
    lines = SubscriptionLineSerializer(many=True)
    total = serializers.IntegerField(help_text='Guaraníes a month.')
    paid_until = serializers.DateField(allow_null=True)
    trial = serializers.BooleanField()
    trial_until = serializers.DateField(allow_null=True)
    suspends_after = serializers.DateField(
        allow_null=True, help_text='Last day of the read-only window.'
    )
    billing = serializers.ChoiceField(choices=('ok', 'due', 'read_only'))
    payment_instructions = serializers.CharField()
    contact_whatsapp = serializers.CharField(help_text="Kyo's WhatsApp, digits only.")
    prices = serializers.DictField(
        child=serializers.IntegerField(),
        help_text='The price list, so screens never hard-code a price.',
    )
