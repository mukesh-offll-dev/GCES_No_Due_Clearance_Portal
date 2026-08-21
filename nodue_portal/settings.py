import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

import cloudinary
import cloudinary.uploader
import cloudinary.api


cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)

# ================= BASE =================
BASE_DIR = Path(__file__).resolve().parent.parent

# ================= SECURITY =================
SECRET_KEY = os.environ.get("SECRET_KEY", "fallback-secret-key")
DEBUG = os.environ.get("DEBUG", "False") == "True"

# In production the app must NOT run with the insecure fallback key — that would
# make session/CSRF signing forgeable. Fail fast at boot instead.
if not DEBUG and SECRET_KEY == "fallback-secret-key":
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "SECRET_KEY must be set to a strong random value in production "
        "(set it in .env; do not use the development fallback)."
    )





ALLOWED_HOSTS = ["*"]


CSRF_TRUSTED_ORIGINS = [
    "https://gces-no-due-clearance-portal.onrender.com",
]

# ================= COOKIE SECURITY =================
# Secure cookies only over HTTPS (production). Disabled on localhost (HTTP).
SESSION_COOKIE_SECURE = not DEBUG   # True in prod, False in local dev
CSRF_COOKIE_SECURE    = not DEBUG   # True in prod, False in local dev

# Behind a TLS-terminating reverse proxy (nginx), trust its forwarded-proto
# header so Django knows the original request was HTTPS. (Correct setting name
# is SECURE_PROXY_SSL_HEADER; the header spelling must match nginx's config.)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ================= HTTPS HARDENING (production, behind TLS) =================
# Enable only once TLS is actually terminating in front of the app, otherwise
# SSL redirect + secure cookies will lock you out over plain HTTP.
ENABLE_HTTPS = os.environ.get("ENABLE_HTTPS", "False") == "True"
if ENABLE_HTTPS and not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", 31536000))  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
# ================= SESSION SETTINGS =================
SESSION_COOKIE_AGE         = 60 * 60 * 2  # 2 hours in seconds
SESSION_SAVE_EVERY_REQUEST = False         # Save only when modified (prevents SQLite lock contention)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False    # Survive tab close within cookie age
SESSION_COOKIE_HTTPONLY    = True          # JS cannot read the session cookie

# ================= APPS =================
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "authentication",
]

# ================= MIDDLEWARE =================
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Per-request structured access log with correlation id + duration.
    "authentication.middleware.RequestLogMiddleware",
    # Anti-caching headers for protected responses.
    "authentication.middleware.NoCacheProtectedMiddleware",
    # Global safety net: converts unhandled/DB errors into safe responses.
    "authentication.middleware.ExceptionHandlingMiddleware",
]

# ================= URL / WSGI =================
ROOT_URLCONF = "nodue_portal.urls"

WSGI_APPLICATION = "nodue_portal.wsgi.application"

# ================= TEMPLATES =================
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],   # optional
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ================= DATABASE =================
# (Django ku mandatory – MongoDB neenga pymongo-la use pannureenga)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 30,  # 30-second busy timeout to prevent lock failures under concurrency
        },
    }
}

# ================= PASSWORD VALIDATION =================
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ================= I18N =================
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ================= STATIC FILES =================
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

 

STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# ================= MEDIA =================

# ================= DEFAULT PK =================
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ================= UPLOAD LIMITS =================
# Reject oversized uploads early (receipts / excel). 10 MB is generous.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.environ.get("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE
DATA_UPLOAD_MAX_NUMBER_FIELDS = 5000  # bulk selects (7.5 scheme, delete) can be large

# ================= LOGGING =================
# Structured logs to stdout — captured by systemd/journald or gunicorn.
# Never log passwords, DOB, or session contents.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
    "loggers": {
        # Application loggers (nodue, nodue.access, nodue.error, nodue.mongo, ...)
        "nodue": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
        # Django's own request errors (4xx/5xx).
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}



