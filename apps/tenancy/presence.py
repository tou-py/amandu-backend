"""
Where a business is and where it lives online, as the links the public page
draws. Pure: reads a tenant, returns strings. The handles and URLs arrive here
already normalised by TenantSerializer (bare handles, full https URLs).
"""

from urllib.parse import quote_plus, urlsplit

from django.conf import settings


def social_links(tenant):
    """The filled networks in a fixed order, each as {network, label, url}."""
    links = []
    if tenant.instagram:
        links.append({
            'network': 'instagram', 'label': f'@{tenant.instagram}',
            'url': f'https://www.instagram.com/{tenant.instagram}/',
        })
    if tenant.facebook:
        links.append({
            'network': 'facebook', 'label': tenant.facebook,
            'url': f'https://www.facebook.com/{tenant.facebook}',
        })
    if tenant.tiktok:
        links.append({
            'network': 'tiktok', 'label': f'@{tenant.tiktok}',
            'url': f'https://www.tiktok.com/@{tenant.tiktok}',
        })
    if tenant.website:
        parts = urlsplit(tenant.website)
        # What a person would say out loud: no scheme, no www, no trailing /.
        label = (parts.netloc.removeprefix('www.') + parts.path).rstrip('/')
        links.append({'network': 'website', 'label': label, 'url': tenant.website})
    return links


def maps_link(tenant):
    """The owner's own pin when they gave one, a search for the address
    otherwise, '' when there is neither."""
    if tenant.maps_url:
        return tenant.maps_url
    if tenant.address:
        return f'https://www.google.com/maps/search/?api=1&query={quote_plus(tenant.address)}'
    return ''


def maps_embed_url(tenant):
    """An embeddable map of the address, or '' without an address or a key:
    the Embed API refuses keyless requests, so there is no map to show then."""
    key = settings.GOOGLE_MAPS_EMBED_KEY
    if not (key and tenant.address):
        return ''
    return f'https://www.google.com/maps/embed/v1/place?key={quote_plus(key)}&q={quote_plus(tenant.address)}'
