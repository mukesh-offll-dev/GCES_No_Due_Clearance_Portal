"""
🔐 Institution logins (Library, College, Hostel, Faculty, each Department) and the
Promotion & Settings password — stored in MongoDB, managed on
Promotion & Settings → Password Management.

Collection ``institution_accounts`` (one doc per login):
    _id            "LIBRARY" | "COLLEGE" | "HOSTEL" | "FACULTY" | "DEPARTMENT:<CODE>" | "PROMOTION"
    role           LIBRARY / COLLEGE / HOSTEL / FACULTY / DEPARTMENT  (None for PROMOTION)
    department     department code for DEPARTMENT logins
    username       login name (case-insensitive match)
    password_hash  Django password hash — used to CHECK logins
    password_enc   the same password encrypted (Fernet, key from SECRET_KEY) — only so
                   faculty can SEE it on the Password Management tab
    updated_at, updated_by

Missing logins are created with the college's default passwords (DEFAULT_LOGINS) at
start-up; after that only the database copy is used, so a password changed on the
Password Management tab is never overwritten.
"""
import base64
import hashlib
import logging
import re
from datetime import datetime, timezone

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password

from .mongo import db

logger = logging.getLogger("nodue")
audit_logger = logging.getLogger("nodue.audit")

accounts_col = db["institution_accounts"]

# Login form "Office" value → role
OFFICE_ROLES = {"library": "LIBRARY", "college": "COLLEGE", "hostel": "HOSTEL", "faculty": "FACULTY"}
OFFICE_LABELS = {"LIBRARY": "Library Office", "COLLEGE": "College Office", "HOSTEL": "Hostel Office",
                 "FACULTY": "Faculty Advisor"}
PROMOTION_ID = "PROMOTION"
PASSWORD_MIN, PASSWORD_MAX = 6, 128
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,40}$")

# Default logins, created only when missing (change them on Password Management).
DEFAULT_LOGINS = {
    "LIBRARY": ('library_admin', 'lib@123'),
    "COLLEGE": ('college_admin', 'col@123'),
    "HOSTEL": ('hostel_admin', 'host@123'),
    "FACULTY": ('faculty_admin', 'fac@123'),
    "DEPARTMENT:CSE": ('cse_admin', 'cse@123'),
    "DEPARTMENT:ECE": ('ece_admin', 'ece@123'),
    "DEPARTMENT:EEE": ('eee_admin', 'eee@123'),
    "DEPARTMENT:CIVIL": ('civil_admin', 'civil@123'),
    "DEPARTMENT:MECH": ('mech_admin', 'mech@123'),
    "DEPARTMENT:MCT": ('mct_admin', 'mct@123'),
}
DEFAULT_PROMOTION_PASSWORD = "gces8301"

_DUMMY_HASH = make_password("never-matches-anything")


# ───────────────────────── encryption (for viewing only) ─────────────────────────
def _fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(("nodue-accounts:" + settings.SECRET_KEY).encode()).digest())
    return Fernet(key)


def _encrypt(password):
    return _fernet().encrypt(password.encode()).decode()


def _decrypt(token):
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:            # SECRET_KEY changed → can't show it (login still works)
        return None


# ───────────────────────── helpers ─────────────────────────
def account_id(role, department=None):
    return f"DEPARTMENT:{str(department).strip().upper()}" if role == "DEPARTMENT" else role


def department_username(code):
    return f"{str(code).strip().lower().replace(' ', '')}_admin"


def validate(username, password, needs_username=True):
    """Return an error message, or None."""
    if needs_username and not _USERNAME_RE.match(username or ""):
        return "Username must be 3–40 characters: letters, numbers, . _ - @ (no spaces)."
    if not password or password != password.strip():
        return "Enter a password without spaces at the start or end."
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        return f"Password must be at least {PASSWORD_MIN} characters."
    return None


def _store(_id, fields, password, actor):
    fields.update({"password_hash": make_password(password), "password_enc": _encrypt(password),
                   "updated_at": datetime.now(timezone.utc), "updated_by": actor})
    accounts_col.update_one({"_id": _id}, {"$set": fields}, upsert=True)
    audit_logger.info("ACCOUNT password set id=%s by=%s", _id, actor)


def set_account(role, username, password, department=None, actor="system"):
    _id = account_id(role, department)
    _store(_id, {"role": role, "department": str(department).strip().upper() if department else None,
                 "username": username.strip()}, password, actor)
    return _id


def set_promotion_password(password, actor="system"):
    _store(PROMOTION_ID, {"role": None, "department": None, "username": ""}, password, actor)


_defaults_done = False


def ensure_default_accounts(force=False):
    """Create any missing login with its default password. Never overwrites an existing one."""
    global _defaults_done
    if _defaults_done and not force:
        return
    for _id, (username, password) in DEFAULT_LOGINS.items():
        if not accounts_col.find_one({"_id": _id}, {"_id": 1}):
            if _id.startswith("DEPARTMENT:"):
                set_account("DEPARTMENT", username, password, department=_id.split(":", 1)[1], actor="default")
            else:
                set_account(_id, username, password, actor="default")
    if not accounts_col.find_one({"_id": PROMOTION_ID}, {"_id": 1}):
        set_promotion_password(DEFAULT_PROMOTION_PASSWORD, actor="default")
    _defaults_done = True


# ───────────────────────── checks ─────────────────────────
def authenticate(office, username, password, department=None):
    """
    Check an Institution Login. ``office`` is the form value (library / college /
    hostel / faculty / department). Returns (role, department_code) or None.
    """
    office = (office or "").strip().lower()
    if office == "department":
        from .departments import get_department_by_code
        code = (department or "").strip().upper()
        dept = get_department_by_code(code) if code else None
        doc = accounts_col.find_one({"_id": account_id("DEPARTMENT", code)}) if code else None
        if not dept or not dept.get("is_active"):
            doc = None                                   # inactive / unknown department cannot log in
        role = "DEPARTMENT"
    else:
        role = OFFICE_ROLES.get(office)
        doc = accounts_col.find_one({"_id": role}) if role else None
        code = None

    password_ok = check_password(password or "", (doc or {}).get("password_hash") or _DUMMY_HASH)
    if doc and password_ok and (username or "").strip().lower() == (doc.get("username") or "").lower():
        return role, (doc.get("department") or code)
    return None


def check_promotion_password(password):
    doc = accounts_col.find_one({"_id": PROMOTION_ID}) or {}
    ok = check_password(password or "", doc.get("password_hash") or _DUMMY_HASH)
    return bool(doc) and ok


def promotion_password_set():
    return accounts_col.count_documents({"_id": PROMOTION_ID}, limit=1) > 0


def logins_configured():
    return accounts_col.count_documents({"_id": {"$ne": PROMOTION_ID}}, limit=1) > 0


# ───────────────────────── Password Management tab ─────────────────────────
def _local(dt):
    """MongoDB returns naive UTC datetimes; show them in India time (naive, like the rest of the app)."""
    from zoneinfo import ZoneInfo
    if not dt:
        return None
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(
        ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)


def list_accounts():
    """Rows for the Password Management tab: every office, every department and the Promotion password."""
    from .departments import get_all_departments
    docs = {d["_id"]: d for d in accounts_col.find({}, {"password_hash": 0})}

    def row(_id, label, kind, active=True, default_username=""):
        d = docs.get(_id) or {}
        return {"id": _id, "label": label, "kind": kind, "active": active, "exists": bool(d),
                "username": d.get("username") or default_username, "password": _decrypt(d.get("password_enc")),
                "updated_at": _local(d.get("updated_at"))}

    rows = [row(r, OFFICE_LABELS[r], "office", default_username=DEFAULT_LOGINS.get(r, (r.lower() + "_admin",))[0])
            for r in ("LIBRARY", "COLLEGE", "HOSTEL", "FACULTY")]
    for dept in sorted(get_all_departments(), key=lambda x: x.get("code", "")):
        code = dept.get("code", "")
        rows.append(row(account_id("DEPARTMENT", code), f"{code} Department", "department",
                        active=bool(dept.get("is_active")), default_username=department_username(code)))
    rows.append(row(PROMOTION_ID, "Promotion & Settings", "promotion"))
    return rows


def update_account(acc_id, username, password, actor="faculty"):
    """Save a new username/password for one login (or the Promotion password). Returns error or None."""
    from .departments import get_department_by_code
    if acc_id == PROMOTION_ID:
        error = validate("", password, needs_username=False)
        if error:
            return error
        set_promotion_password(password, actor=actor)
        return None
    error = validate(username, password)
    if error:
        return error
    if acc_id in OFFICE_LABELS:
        set_account(acc_id, username, password, actor=actor)
        return None
    if acc_id.startswith("DEPARTMENT:"):
        code = acc_id.split(":", 1)[1]
        if not get_department_by_code(code):
            return "That department no longer exists."
        set_account("DEPARTMENT", username, password, department=code, actor=actor)
        return None
    return "Unknown login."


def rename_department_account(old_code, new_code):
    """Keep a department's login when its code is edited."""
    old_id, new_id = account_id("DEPARTMENT", old_code), account_id("DEPARTMENT", new_code)
    if old_id == new_id:
        return
    doc = accounts_col.find_one({"_id": old_id})
    if not doc or accounts_col.find_one({"_id": new_id}, {"_id": 1}):
        return
    doc["_id"] = new_id
    doc["department"] = new_id.split(":", 1)[1]
    accounts_col.insert_one(doc)
    accounts_col.delete_one({"_id": old_id})
    audit_logger.info("ACCOUNT renamed %s -> %s", old_id, new_id)


def delete_department_account(code):
    accounts_col.delete_one({"_id": account_id("DEPARTMENT", code)})
