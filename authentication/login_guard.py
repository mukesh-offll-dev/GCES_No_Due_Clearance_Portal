"""
🛡️ Wrong-password lockout for every login form (student, institution, Promotion & Settings).

5 wrong passwords for the same account within 15 minutes → that account is locked
for 15 minutes. Counted per account (Register Number / office+username), not per
IP, because a whole college can share one public IP. Stored in MongoDB
``login_attempts`` (TTL index on ``purge_at``) so all workers share it.
"""
import logging
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from .mongo import db

logger = logging.getLogger("nodue")
audit_logger = logging.getLogger("nodue.audit")

attempts_col = db["login_attempts"]
MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
LOCK_FOR = timedelta(minutes=15)


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def locked_seconds(key):
    """Seconds left on a lock for ``key`` (0 = not locked). Fails open if the DB errors."""
    try:
        doc = attempts_col.find_one({"_id": key}, {"locked_until": 1})
    except PyMongoError as exc:
        logger.warning("login_guard lookup failed: %s", exc)
        return 0
    until = _utc((doc or {}).get("locked_until"))
    if not until:
        return 0
    left = (until - datetime.now(timezone.utc)).total_seconds()
    return int(left) + 1 if left > 0 else 0


def record_failure(key):
    """Count one wrong password. Returns True when this failure locks the account."""
    now = datetime.now(timezone.utc)
    try:
        doc = attempts_col.find_one({"_id": key}) or {}
        until = _utc(doc.get("locked_until"))
        if until and until > now:
            return True                                   # still locked
        first = _utc(doc.get("first_at"))
        if not first or now - first > WINDOW or until:    # fresh window (also after a lock ended)
            attempts_col.update_one({"_id": key}, {"$set": {
                "count": 1, "first_at": now, "locked_until": None, "purge_at": now + WINDOW + LOCK_FOR}},
                upsert=True)
            return False
        doc = attempts_col.find_one_and_update(
            {"_id": key}, {"$inc": {"count": 1}, "$set": {"purge_at": now + WINDOW + LOCK_FOR}},
            return_document=ReturnDocument.AFTER)
        if doc and doc.get("count", 0) >= MAX_FAILURES:
            attempts_col.update_one({"_id": key}, {"$set": {"locked_until": now + LOCK_FOR,
                                                            "purge_at": now + LOCK_FOR + WINDOW}})
            audit_logger.warning("LOGIN LOCKED key=%s for %s", key, LOCK_FOR)
            return True
    except PyMongoError as exc:
        logger.warning("login_guard record failed: %s", exc)
    return False


def clear(key):
    try:
        attempts_col.delete_one({"_id": key})
    except PyMongoError as exc:
        logger.warning("login_guard clear failed: %s", exc)


def lock_message(seconds):
    minutes = max(1, (seconds + 59) // 60)
    return f"Too many wrong attempts. Please try again in {minutes} minute{'s' if minutes != 1 else ''}."
