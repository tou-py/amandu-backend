"""
Images as the API sees them: an object key in a nullable column, never the
bytes. Logo, member photo and service image all go through the same three
pieces here, so the rules cannot drift between them:

- `KeyField(kind)`: the write-only `*_key` on a serializer. The attach check.
- `UrlField(source=...)`: the read-only `*_url` next to it.
- `ImageKeysMixin`: on the serializer, deletes the object a save replaced or
  cleared, after the transaction commits.

Keys are `<tenant_id>/<kind>/<uuid4>.<webp|jpg>`, the extension per TYPES.
The tenant prefix is what isolates businesses from each other; the uuid makes
keys unguessable and never reused, which the year-long immutable cache
depends on.
"""

import logging
import re
import uuid

from django.conf import settings
from django.db import transaction
from rest_framework import serializers

from apps.commons import storage

logger = logging.getLogger(__name__)

# Bytes after the browser compressed it. A logo and a 512px face are small; a
# service image is shown large on the public page.
CAPS = {
    'logo': 300 * 1024,
    'photo': 300 * 1024,
    'service': 1024 * 1024,
}


# What an upload may be, and the extension its key gets. JPEG because Safari
# cannot encode WebP from a canvas.
TYPES = {'image/webp': 'webp', 'image/jpeg': 'jpg'}
# The name new_key mints: a uuid4, canonical or bare hex, then an extension.
NAME = r'(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(?:' + '|'.join(TYPES.values()) + ')'


def new_key(tenant, kind, content_type):
    return f'{tenant.pk}/{kind}/{uuid.uuid4()}.{TYPES[content_type]}'


def public_url(key):
    """Where an image is read from. The serializers and the public page both
    build URLs here, so they cannot disagree."""
    return f'{settings.S3_PUBLIC_URL.rstrip("/")}/{key}' if key else None


def discard(key):
    """
    Delete `key` once the current transaction commits, so a request that rolls
    back never deletes the image that is still attached. Best effort: a failure
    is logged and the object left behind, which costs fractions of a cent --
    never worth failing the request over.
    """
    if not key:
        return

    def run():
        try:
            storage.delete(key)
        except Exception:
            logger.exception('Could not delete image %s', key)

    transaction.on_commit(run)


class KeyField(serializers.CharField):
    """
    The write-only key an owner is attached to after the browser uploaded it.
    Accepts only an object that is in THIS tenant's space, of THIS field's
    kind, actually in the bucket, of the type its extension names, and within
    the cap: the server never sees the bytes, so this HEAD is the whole check. `null` removes the image.

    Reads the active tenant from the serializer context's request, which every
    tenant-plane view provides (HasActiveMembership sets request.tenant).
    """

    def __init__(self, kind, **kwargs):
        self.kind = kind
        kwargs.setdefault('write_only', True)
        kwargs.setdefault('required', False)
        kwargs.setdefault('allow_null', True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        key = super().to_internal_value(data)
        if self.parent.instance is None:
            # An upload is signed for an owner that exists (a service's id), so
            # an image is attached after creating, never with it.
            raise serializers.ValidationError('Attach the image once this exists.', code='image_on_create')
        tenant = self.context['request'].tenant
        # The whole shape, not a prefix: `<mine>/logo/../../<theirs>/...` starts
        # right and is somebody else's object.
        if not re.fullmatch(rf'{re.escape(str(tenant.pk))}/{self.kind}/{NAME}', key):
            raise serializers.ValidationError('Not an upload of this business for this image.', code='image_foreign')
        # One owner per key: replacing one owner's image deletes the object,
        # which another owner showing it would then lose. The unique column
        # backs this up; this check turns it into a 400 on the field.
        owners = self.parent.Meta.model.objects.filter(**{self.source: key})
        if owners.exclude(pk=self.parent.instance.pk).exists():
            raise serializers.ValidationError('This image is already attached elsewhere.', code='image_taken')
        meta = storage.head(key)
        if meta is None:
            raise serializers.ValidationError('Nothing was uploaded under this key.', code='image_missing')
        # The type the bucket stored must be the one the key's extension names.
        if TYPES.get(meta['content_type']) != key.rsplit('.', 1)[1] or meta['size'] > CAPS[self.kind]:
            raise serializers.ValidationError(
                'The upload is not a WebP or JPEG of its key, within the size limit.', code='image_invalid',
            )
        return key


class UrlField(serializers.ReadOnlyField):
    """The read-only `*_url` for a key column: `UrlField(source='image_key')`."""

    def to_representation(self, value):
        return public_url(value)


class ImageKeysMixin:
    """
    For a ModelSerializer with KeyFields: when an update replaces or clears a
    key, the previous object is discarded (deleted after commit). A request
    that fails validation never reaches update(), so it deletes nothing.
    """

    def update(self, instance, validated_data):
        old = {
            name: getattr(instance, name)
            for name, field in self.fields.items()
            if isinstance(field, KeyField) and name in validated_data
        }
        instance = super().update(instance, validated_data)
        for name, key in old.items():
            if key != getattr(instance, name):
                discard(key)
        return instance
