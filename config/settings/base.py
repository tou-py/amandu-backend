"""
Shared Django settings. Never used directly: pick an environment module
(development, test, production) through DJANGO_SETTINGS_MODULE.

Anything that differs between deployments is required from the environment, with no
fallback: a missing variable fails at boot naming itself, in every environment
equally. Copy .env.example to .env to work locally. Only values that are safe
everywhere (ports, regions, timeouts) carry a default.

https://docs.djangoproject.com/en/6.0/ref/settings/
"""

from datetime import timedelta
from pathlib import Path
import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
env = environ.Env()
environ.Env.read_env(BASE_DIR / '.env')

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env.str('SECRET_KEY')

# Overridden per environment.
DEBUG = False

ALLOWED_HOSTS = env.list('ALLOWED_HOSTS')

AUTH_USER_MODEL = 'accounts.CustomUser'

# Django still defaults to AutoField (32-bit) and warns per app (models.W042).
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Application definition

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Postgres-only field types and constraints. Scheduling needs ExclusionConstraint
    # to forbid overlapping appointments, which no other backend implements -- which
    # is also why the test settings now talk to a real Postgres.
    'django.contrib.postgres',
]

THIRD_PARTY_APPS = [
    'corsheaders',
    'phonenumber_field',
    'rest_framework',
    'drf_spectacular',
]

LOCAL_APPS = [
    'apps.commons',
    'apps.tenancy',
    'apps.accounts',
    'apps.scheduling',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': env.str('POSTGRES_DB'),
        'USER': env.str('POSTGRES_USER'),
        'PASSWORD': env.str('POSTGRES_PASSWORD'),
        'HOST': env.str('POSTGRES_HOST'),
        'PORT': env.int('POSTGRES_PORT', default=5432),
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# S3 environment keys
AWS_S3_ENDPOINT_URL = env.str('AWS_S3_ENDPOINT_URL')
AWS_ACCESS_KEY_ID = env.str('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = env.str('AWS_SECRET_ACCESS_KEY')
AWS_STORAGE_BUCKET_NAME = env.str('AWS_STORAGE_BUCKET_NAME')
AWS_S3_REGION_NAME = env.str('AWS_S3_REGION_NAME', default='us-east-1')

# MinIO serves buckets as a path, not a subdomain.
AWS_S3_ADDRESSING_STYLE = 'path'
# without this boto3 signs with the legacy V2 scheme, which trips browser CORS
# preflights on direct uploads.
AWS_S3_SIGNATURE_VERSION = 's3v4'
# Private bucket: .url() returns a presigned GET that expires, so downloads need no endpoint of our own.
AWS_QUERYSTRING_AUTH = True
AWS_QUERYSTRING_EXPIRE = env.int('AWS_QUERYSTRING_EXPIRE', default=3600)

STORAGES = {
    'default': {
        'BACKEND': 'storages.backends.s3.S3Storage',
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}


# CORS
CORS_ALLOWED_ORIGINS = env.list('CORS_ALLOWED_ORIGINS')

# Cache and Celery broker

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': env.str('REDIS_URL'),
    }
}


# Django REST Framework
# Fail closed by default: every endpoint requires authentication unless it opts
# out explicitly (the login/refresh views set their own empty permissions).
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    # Every list is paginated: an unbounded agenda or client list is a slow query
    # waiting for the first busy tenant. Responses become {count, next, previous,
    # results}.
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 50,
    # OpenAPI 3 schema generation. The subclass documents the X-Tenant-ID header
    # on the endpoints that resolve a tenant through it.
    'DEFAULT_SCHEMA_CLASS': 'apps.tenancy.schema.TenantHeaderAutoSchema',
    # Rate limiting. The default anon/user rates are a broad backstop; the tight
    # scoped rates below live on the credential endpoints (login, invitation
    # accept) that a brute-force attack actually targets. State is kept in the
    # cache (Redis in prod), so it holds across processes and restarts.
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        # Covers every anonymous endpoint we don't scope explicitly -- notably
        # token refresh, which takes a refresh token and is therefore a
        # credential endpoint we can't scope without subclassing SimpleJWT.
        'anon': '60/min',
        'user': '1000/min',
        # Password guessing: five tries a minute per IP, counted whether the
        # attempt succeeds or 401s, since throttling runs before the view.
        'login': '5/min',
        # Token guessing on the public accept endpoint.
        'accept-invitation': '10/min',
    },
}

# drf-spectacular (OpenAPI 3). Schema at /api/schema/, Swagger UI at /api/docs/.
SPECTACULAR_SETTINGS = {
    'TITLE': 'Amandu API',
    'DESCRIPTION': 'Multi-tenant appointment scheduling API. Authenticate with a '
                   'JWT (POST /api/auth/login/) and send X-Tenant-ID to pick the '
                   'tenant you act for.',
    'VERSION': '0.1.0',
    # The Swagger/Redoc pages already render the schema; don't also inline it there.
    'SERVE_INCLUDE_SCHEMA': False,
}

# JWT
# The access token carries IDENTITY ONLY — no tenant, no role. The active tenant
# travels in the X-Tenant-ID header and is validated against a live Membership on
# every request, so authorization is never frozen into the token.
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=1),
}

# Email
# Console by default: safe everywhere, prints to stdout in dev and never sends a
# real message by accident. Production sets EMAIL_BACKEND to SMTP via the
# environment along with the host/credentials below.
EMAIL_BACKEND = env.str('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = env.str('EMAIL_HOST', default='localhost')
EMAIL_PORT = env.int('EMAIL_PORT', default=25)
EMAIL_HOST_USER = env.str('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = env.str('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = env.bool('EMAIL_USE_TLS', default=False)
DEFAULT_FROM_EMAIL = env.str('DEFAULT_FROM_EMAIL', default='no-reply@amandu.local')

# The invited person opens this frontend page, which reads the token from the query
# string and POSTs it to the accept endpoint. That frontend is not ours, so its URL
# is configuration, not a hardcoded route.
INVITATION_ACCEPT_URL = env.str(
    'INVITATION_ACCEPT_URL', default='http://localhost:3000/invitations/accept'
)
