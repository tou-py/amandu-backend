"""
The image bucket, and the only code that talks to it: sign an upload, look an
object up, delete one. Three calls, so boto3 directly rather than
django-storages -- no file ever passes through Django, and a storage backend
would be machinery nothing uses.

This module is also the test seam: the suite replaces `head` and `delete` with
a dict (the `bucket` fixture in conftest.py) and lets `presign_put` sign for
real, since that is offline computation.
"""

from functools import cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

# The year-long `immutable` cache is only safe because keys are never reused:
# replacing an image always mints a new key. Never write to an existing key.
CACHE_CONTROL = 'public, max-age=31536000, immutable'
CONTENT_TYPE = 'image/webp'
# Long enough to compress-then-upload on a slow phone, short enough that a
# leaked URL is dead by the time anyone finds it.
EXPIRES = 300  # seconds


@cache
def _client(endpoint):
    return boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        region_name=settings.S3_REGION,
        # Path style: MinIO on localhost has no per-bucket subdomain, and R2
        # accepts both.
        config=Config(signature_version='s3v4', s3={'addressing_style': 'path'}),
    )


def presign_put(key, size):
    """
    A URL the browser PUTs exactly one WebP of exactly `size` bytes to, and the
    headers it must send. R2 has no presigned POST policies (no
    content-length-range), so the type, the exact length and the cache header
    are pinned in the signature instead: any other value fails the PUT.
    """
    headers = {
        'Content-Type': CONTENT_TYPE,
        'Content-Length': str(size),
        'Cache-Control': CACHE_CONTROL,
    }
    url = _client(settings.S3_PUBLIC_ENDPOINT).generate_presigned_url(
        'put_object',
        Params={
            'Bucket': settings.S3_BUCKET,
            'Key': key,
            'ContentType': CONTENT_TYPE,
            'ContentLength': size,
            'CacheControl': CACHE_CONTROL,
        },
        ExpiresIn=EXPIRES,
    )
    return url, headers


def head(key):
    """{'size', 'content_type'} of the object, or None when there is none."""
    try:
        meta = _client(settings.S3_ENDPOINT).head_object(Bucket=settings.S3_BUCKET, Key=key)
    except ClientError as error:
        if error.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
            return None
        raise
    return {'size': meta['ContentLength'], 'content_type': meta['ContentType']}


def delete(key):
    _client(settings.S3_ENDPOINT).delete_object(Bucket=settings.S3_BUCKET, Key=key)
