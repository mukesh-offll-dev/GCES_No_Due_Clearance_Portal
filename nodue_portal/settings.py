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





ALLOWED_HOSTS = ["*"]


CSRF_TRUSTED_ORIGINS = [
    "https://gces-no-due-clearance-portal.onrender.com",
]

# ================= COOKIE SECURITY =================
# Secure cookies only over HTTPS (production). Disabled on localhost (HTTP).
SESSION_COOKIE_SECURE = not DEBUG   # True in prod, False in local dev
CSRF_COOKIE_SECURE    = not DEBUG   # True in prod, False in local dev

# ================= SESSION SETTINGS =================
SESSION_COOKIE_AGE         = 60 * 60 * 2  # 2 hours in seconds
SESSION_SAVE_EVERY_REQUEST = True          # Slide expiry window on every request
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



