"""
🔑 Student password reset by email (EmailJS).

Flow
    1. Student enters their Register Number on /student/forgot-password/.
    2. A 6-digit code is emailed (EmailJS REST API) to the email saved on their
       profile. Only a keyed hash of the code is stored (collection
       ``password_resets``, one doc per student, TTL-purged).
    3. On /student/reset-password/ they enter the code + a new password.
       A correct code sets the new password, bumps ``password_version`` (which
       logs out every other session of that student, see decorators.py) and
       deletes the code.

Limits
    • Code valid for PASSWORD_RESET_CODE_MINUTES (default 10).
    • 5 wrong codes → the code is cancelled; a new one must be requested.
    • A new code at most every 60 s, and at most 5 codes per hour per student.

EmailJS template variables sent (use whichever your template needs):
    to_email / email, to_name / name, passcode / code / otp, time,
    valid_minutes, reg_no, app_name.
The template's "To Email" field must be {{to_email}} (or {{email}}).
"""
import hashlib
import hmac
import json
import logging
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .mongo import db

logger = logging.getLogger("nodue")
audit_logger = logging.getLogger("nodue.audit")

password_resets = db["password_resets"]

EMAILJS_URL = "https://api.emailjs.com/api/v1.0/email/send"
MAX_VERIFY_ATTEMPTS = 5
RESEND_GAP = timedelta(seconds=60)
MAX_SENDS_PER_HOUR = 5
_IST = ZoneInfo("Asia/Kolkata")


# ───────────────────────── helpers ─────────────────────────
def mail_enabled():
    return bool(getattr(settings, "APP_MAIL_ENABLED", False))


def code_minutes():
    return max(1, int(getattr(settings, "PASSWORD_RESET_CODE_MINUTES", 10)))


def clean_email(value):
    """Return (email, error). Lower-cased, trimmed, validated."""
    email = (value or "").strip().lower()
    if not email:
        return "", "Enter your email address."
    if len(email) > 254:
        return "", "Email address is too long."
    try:
        validate_email(email)
    except ValidationError:
        return "", "Enter a valid email address (e.g. name@gmail.com)."
    return email, None


def mask_email(email):
    """ar****@gmail.com — enough for the student to recognise it."""
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    shown = local[:2] if len(local) > 2 else local[:1]
    return f"{shown}{'*' * max(3, len(local) - len(shown))}@{domain}"


def _code_hash(student_id, code):
    key = settings.SECRET_KEY.encode()
    return hmac.new(key, f"pwreset:{student_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


# ───────────────────────── EmailJS ─────────────────────────
def send_email(template_params):
    """POST to the EmailJS REST API. Returns (ok, detail)."""
    payload = {
        "service_id": settings.EMAILJS_SERVICE_ID,
        "template_id": settings.EMAILJS_TEMPLATE_ID,
        "user_id": settings.EMAILJS_PUBLIC_KEY,
        "accessToken": settings.EMAILJS_PRIVATE_KEY,
        "template_params": template_params,
    }
    req = urllib.request.Request(
        EMAILJS_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "gces-nodue-portal"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300, resp.read(300).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode(errors="replace")
        logger.error("EmailJS rejected the email (HTTP %s): %s", exc.code, detail)
        return False, detail
    except Exception as exc:   # network / DNS / timeout
        logger.error("EmailJS request failed: %s", exc)
        return False, str(exc)


# ───────────────────────── reset flow ─────────────────────────
def request_code(student):
    """
    Create a code for ``student`` (a students doc) and email it.
    Returns dict(ok, message, wait_seconds).
    """
    sid = student["_id"]
    email = (student.get("email") or "").strip()
    now = datetime.now(timezone.utc)
    existing = password_resets.find_one({"_id": sid}) or {}

    last = _utc(existing.get("last_sent_at"))
    if last and now - last < RESEND_GAP:
        wait = int((RESEND_GAP - (now - last)).total_seconds()) + 1
        return {"ok": False, "wait_seconds": wait,
                "message": f"A code was just sent. Please wait {wait} seconds before asking for another."}

    window_start = _utc(existing.get("window_start"))
    sends = existing.get("sends", 0) if window_start and now - window_start < timedelta(hours=1) else 0
    if sends >= MAX_SENDS_PER_HOUR:
        return {"ok": False, "wait_seconds": 0,
                "message": "Too many codes were requested. Please try again after an hour."}

    code = f"{secrets.randbelow(1_000_000):06d}"
    minutes = code_minutes()
    expires = now + timedelta(minutes=minutes)

    ok, detail = send_email({
        "to_email": email, "email": email,
        "to_name": student.get("name") or "Student", "name": student.get("name") or "Student",
        "passcode": code, "code": code, "otp": code,
        "time": expires.astimezone(_IST).strftime("%I:%M %p"),
        "valid_minutes": str(minutes),
        "reg_no": student.get("reg_no", ""),
        "app_name": "GCES No Due Clearance Portal",
    })
    if not ok:
        audit_logger.warning("PASSWORD RESET email failed reg_no=%s", student.get("reg_no"))
        return {"ok": False, "wait_seconds": 0,
                "message": "We couldn't send the email right now. Please try again in a few minutes "
                           "or contact your Faculty Advisor."}

    password_resets.update_one({"_id": sid}, {"$set": {
        "code_hash": _code_hash(sid, code),
        "email": email,
        "expires_at": expires,
        "attempts": 0,
        "last_sent_at": now,
        "window_start": window_start if sends else now,
        "sends": sends + 1,
        "purge_at": now + timedelta(hours=1),     # TTL: doc (and rate-limit counters) vanish after an hour
    }}, upsert=True)
    audit_logger.info("PASSWORD RESET code sent reg_no=%s", student.get("reg_no"))
    return {"ok": True, "wait_seconds": int(RESEND_GAP.total_seconds()),
            "message": f"We sent a 6-digit code to {mask_email(email)}. It is valid for {minutes} minutes."}


def check_code(student_id, code):
    """
    Verify a code. Returns (ok, error_message). A wrong code counts one attempt;
    after MAX_VERIFY_ATTEMPTS the code is cancelled. Does NOT consume a correct
    code (call ``consume`` after the password is saved).
    """
    code = re.sub(r"\s+", "", code or "")
    doc = password_resets.find_one({"_id": student_id})
    if not doc or not doc.get("code_hash"):
        return False, "No active code. Please request a new code."
    if datetime.now(timezone.utc) > _utc(doc.get("expires_at")):
        password_resets.update_one({"_id": student_id}, {"$unset": {"code_hash": ""}})
        return False, "This code has expired. Please request a new code."
    if not (code.isdigit() and len(code) == 6) or not hmac.compare_digest(doc["code_hash"], _code_hash(student_id, code)):
        attempts = doc.get("attempts", 0) + 1
        if attempts >= MAX_VERIFY_ATTEMPTS:
            password_resets.update_one({"_id": student_id}, {"$unset": {"code_hash": ""}, "$set": {"attempts": attempts}})
            return False, "Too many wrong codes. Please request a new code."
        password_resets.update_one({"_id": student_id}, {"$set": {"attempts": attempts}})
        left = MAX_VERIFY_ATTEMPTS - attempts
        return False, f"That code is not correct. {left} attempt{'s' if left != 1 else ''} left."
    return True, None


def consume(student_id):
    """Cancel the code after a successful reset (keeps the rate-limit counters)."""
    password_resets.update_one({"_id": student_id}, {"$unset": {"code_hash": "", "expires_at": ""}})
