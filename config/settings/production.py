"""Production: TLS terminated by Traefik, uploaded media on Cloudflare R2.

Credentials and hosts are not re-declared here: base.py already requires every one of
them from the environment, so there is no permissive fallback left to override. The
object-storage keys are the exception -- they live here rather than in base.py so
that development and tests, which write to local disk, need no R2 account at all.
"""

from .base import *  # noqa: F403

DEBUG = False

# config for Traefik
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 180
SECURE_HSTS_INCLUDE_SUBDOMAINS = True


# Uploaded media: Cloudflare R2 over the S3 API.
# Endpoint is https://<account-id>.r2.cloudflarestorage.com
AWS_S3_ENDPOINT_URL = env.str('AWS_S3_ENDPOINT_URL')  # noqa: F405
AWS_ACCESS_KEY_ID = env.str('AWS_ACCESS_KEY_ID')  # noqa: F405
AWS_SECRET_ACCESS_KEY = env.str('AWS_SECRET_ACCESS_KEY')  # noqa: F405
AWS_STORAGE_BUCKET_NAME = env.str('AWS_STORAGE_BUCKET_NAME')  # noqa: F405

# R2 has no notion of regions; it rejects a real one. 'auto' is the value it expects.
AWS_S3_REGION_NAME = 'auto'
# Path style is mandatory here: the R2 endpoint is a single host with no wildcard
# DNS, so virtual-hosted addressing (<bucket>.<account>.r2.cloudflarestorage.com)
# does not resolve.
AWS_S3_ADDRESSING_STYLE = 'path'
# Without this boto3 signs with the legacy V2 scheme, which R2 does not accept.
AWS_S3_SIGNATURE_VERSION = 's3v4'
# Private bucket: .url() returns a presigned GET that expires, so the browser
# fetches images straight from R2 and the image bytes never pass back through us.
AWS_QUERYSTRING_AUTH = True
AWS_QUERYSTRING_EXPIRE = env.int('AWS_QUERYSTRING_EXPIRE', default=3600)  # noqa: F405

STORAGES = {
    **STORAGES,  # noqa: F405
    'default': {'BACKEND': 'storages.backends.s3.S3Storage'},
}
