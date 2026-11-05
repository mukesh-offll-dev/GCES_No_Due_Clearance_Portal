"""
🔐 Hostel Office SSO (single sign-on from the Hostel Admin App).

The Hostel Admin App signs a short-lived, one-time token with a secret that
only it and this portal know (env var ``HOSTEL_SSO_SECRET``). This endpoint
verifies the token and starts a normal Hostel Office session — exactly the
session the regular login form creates (``session["role"] = "HOSTEL"``), so
every existing permission check keeps working unchanged.

Request (GET query string or POST form fields):
    ts     Unix time in seconds (UTC) when the link was made
    nonce  random, single-use string: 16–128 chars of [A-Za-z0-9_-]
    user   optional username for the audit log: up to 64 chars of [A-Za-z0-9._@-]
    sig    lowercase hex HMAC-SHA256(HOSTEL_SSO_SECRET, message)

    message = "HOSTEL_SSO\\n" + ts + "\\n" + nonce + "\\n" + user
              (user is "" when not sent)

Security:
    • Signature compared in constant time.
    • Token valid for HOSTEL_SSO_MAX_AGE seconds (default 60) — small clock
      skew into the future is tolerated.
    • Each nonce works once (stored in MongoDB ``sso_nonces`` with a TTL index),
      so a captured link cannot be replayed.
    • Any existing session is discarded and a new session id issued (no
      session fixation, no privilege mixing with a student/other office).
    • Failures return one generic 403 page; the reason is only in the audit log.
    • If HOSTEL_SSO_SECRET is not configured the endpoint does not exist (404).
"""
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from pymongo.errors import DuplicateKeyError, PyMongoError

from .decorators import _add_no_cache_headers
from .mongo import db, institution_logs

logger = logging.getLogger("nodue")
audit_logger = logging.getLogger("nodue.audit")

sso_nonces = db["sso_nonces"]

SSO_MESSAGE_PREFIX = "HOSTEL_SSO"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{0,64}$")
_SIG_RE = re.compile(r"^[0-9a-f]{64}$")
_FUTURE_SKEW_SECONDS = 30   # tolerate the other server's clock running slightly ahead
_MIN_SECRET_LENGTH = 32


def sso_message(ts, nonce, user=""):
    """The exact string the Hostel Admin App must sign."""
    return f"{SSO_MESSAGE_PREFIX}\n{ts}\n{nonce}\n{user}"


def sign(secret, ts, nonce, user=""):
    """Reference signer (used by tests; mirrors what the Hostel Admin App does)."""
    return hmac.new(secret.encode("utf-8"), sso_message(ts, nonce, user).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _secret():
    secret = getattr(settings, "HOSTEL_SSO_SECRET", "") or ""
    return secret if len(secret) >= _MIN_SECRET_LENGTH else ""


def _deny(request, reason, user=""):
    audit_logger.warning("SSO hostel denied reason=%s user=%s ip=%s",
                         reason, user or "-", request.META.get("REMOTE_ADDR"))
    response = HttpResponse(
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Sign-in link not valid</title>"
        "<div style='font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem;text-align:center'>"
        "<h1 style='font-size:1.25rem'>This sign-in link is not valid</h1>"
        "<p style='color:#555'>It may have expired or already been used. "
        "Please open the No Due Portal again from the Hostel Admin App, or log in normally.</p>"
        "<p><a href='/' style='color:#b42318;font-weight:600'>Go to login</a></p></div>",
        status=403,
    )
    response["Referrer-Policy"] = "no-referrer"
    return _add_no_cache_headers(response)


@csrf_exempt                      # cross-site POST from the Hostel Admin App; the HMAC is the proof
@require_http_methods(["GET", "POST"])
def hostel_sso_login(request):
    secret = _secret()
    if not secret:
        # SSO not configured on this server → behave as if the URL does not exist.
        # (Returned, not raised: the global exception middleware would turn Http404 into a 500.)
        return HttpResponseNotFound("Not found")

    params = request.POST if request.method == "POST" else request.GET
    ts_raw = (params.get("ts") or "").strip()
    nonce = (params.get("nonce") or "").strip()
    user = (params.get("user") or "").strip()
    sig = (params.get("sig") or "").strip().lower()

    # 1. Shape checks (cheap, before any crypto / DB work)
    if not ts_raw.isdigit() or not _NONCE_RE.match(nonce) or not _USER_RE.match(user) or not _SIG_RE.match(sig):
        return _deny(request, "malformed", user if _USER_RE.match(user) else "")

    # 2. Signature (constant time)
    expected = sign(secret, ts_raw, nonce, user)
    if not hmac.compare_digest(expected, sig):
        return _deny(request, "bad_signature", user)

    # 3. Freshness
    max_age = int(getattr(settings, "HOSTEL_SSO_MAX_AGE", 60))
    age = int(time.time()) - int(ts_raw)
    if age > max_age or age < -_FUTURE_SKEW_SECONDS:
        return _deny(request, "expired", user)

    # 4. One-time use — the insert is atomic, so two racing requests cannot both win
    try:
        sso_nonces.insert_one({
            "_id": nonce,
            "user": user,
            "used_at": datetime.now(timezone.utc),
            # kept a little longer than a token can live, then removed by the TTL index
            "expire_at": datetime.now(timezone.utc) + timedelta(seconds=max_age + _FUTURE_SKEW_SECONDS + 60),
        })
    except DuplicateKeyError:
        return _deny(request, "replay", user)
    except PyMongoError as exc:
        logger.error("SSO nonce store unavailable: %s", exc)
        return _deny(request, "nonce_store_error", user)

    # 5. Fresh Hostel Office session (drop whatever was there before)
    request.session.flush()
    request.session["role"] = "HOSTEL"
    request.session["login_method"] = "SSO"
    if user:
        request.session["sso_user"] = user

    try:
        institution_logs.insert_one({
            "office": "HOSTEL",
            "username": user or "hostel_sso",
            "login_method": "SSO",
            "login_time": datetime.now(),
        })
    except Exception as exc:
        logger.warning("Failed to record hostel SSO login log: %s", exc)
    audit_logger.info("SSO hostel login user=%s ip=%s", user or "-", request.META.get("REMOTE_ADDR"))

    response = redirect("hostel_dashboard")
    response["Referrer-Policy"] = "no-referrer"
    return _add_no_cache_headers(response)
