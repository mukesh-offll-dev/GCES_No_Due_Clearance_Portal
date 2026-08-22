"""
Background maintenance for the No Due portal.

Previously `reset_expired_no_dues()` ran synchronously at the top of almost
every dashboard request AND made blocking Cloudinary network calls in a loop —
so one slow dashboard hit could stall a worker thread for seconds.

This module splits that work:

  * fast_cooldown_reset()  — a single bulk update, cheap enough to keep inline
    on request paths so students never see a stale cooldown.
  * run_maintenance_cycle() — the heavy part (expire stale PENDING, delete
    orphaned Cloudinary receipts). Runs in the background scheduler thread and
    via the `run_maintenance` management command (cron / systemd timer).

Concurrency: run_maintenance_cycle() takes a short-lived MongoDB lock so that
across N gunicorn workers only ONE actually performs a cycle at a time.
"""
import logging
from datetime import datetime, timedelta, timezone

from pymongo.errors import PyMongoError

from .mongo import no_due_col, portal_settings

logger = logging.getLogger("nodue.maintenance")

# How stale a PENDING request may get before it reverts to NOT_SENT.
PENDING_TTL = timedelta(days=1)
# Cloudinary deletes are batched per cycle to bound runtime.
MAX_CLOUDINARY_DELETES_PER_CYCLE = 200


def fast_cooldown_reset():
    """
    Clear elapsed 24h cooldowns in a single bulk update. Cheap and idempotent —
    safe to call inline on hot request paths. Cooldown datetimes may be naive or
    tz-aware, so we compare against both a naive and an aware 'now'.
    """
    now_utc = datetime.now(timezone.utc)
    now_naive = datetime.now()
    try:
        # Find expiring cooldowns to broadcast live unlock to students
        expired_docs = list(no_due_col.find(
            {
                "cooldown_expiry": {"$exists": True, "$ne": None},
                "$or": [
                    {"cooldown_expiry": {"$lte": now_utc}},
                    {"cooldown_expiry": {"$lte": now_naive}},
                ],
            },
            {"student_id": 1, "office": 1}
        ))

        if expired_docs:
            no_due_col.update_many(
                {
                    "cooldown_expiry": {"$exists": True, "$ne": None},
                    "$or": [
                        {"cooldown_expiry": {"$lte": now_utc}},
                        {"cooldown_expiry": {"$lte": now_naive}},
                    ],
                },
                {
                    "$set": {
                        "attempts_used": 0,
                        "status": "NOT_SENT",
                        "reject_reason": None,
                        "updated_at": now_naive,
                    },
                    "$unset": {"cooldown_expiry": "", "second_rejection_at": ""},
                },
            )
            # Broadcast to affected students
            try:
                from .events import notify_student
                for doc in expired_docs:
                    sid = doc.get("student_id")
                    office = doc.get("office")
                    notify_student(sid, "cooldown_expired", {
                        "office": office,
                        "status": "NOT_SENT",
                        "attempts_used": 0,
                        "attempts_remaining": 2,
                    })
                    notify_student(sid, "request_status_updated", {
                        "office": office,
                        "status": "NOT_SENT",
                        "attempts_used": 0,
                        "attempts_remaining": 2,
                        "is_cooldown_active": False,
                        "cooldown_expiry_iso": "",
                    })
            except Exception as e:
                logger.warning("[WS] Failed to notify students of cooldown expiry: %s", e)

    except PyMongoError as exc:
        logger.error("fast_cooldown_reset failed: %s", exc)


def _check_auto_disable_access():
    """
    Check if scheduled auto-disable time for No Due Access has elapsed.
    If so, turn off access in MongoDB and broadcast live event to students & faculty.
    """
    now_utc = datetime.now(timezone.utc)
    try:
        settings_doc = portal_settings.find_one({"_id": "global_config"})
        if not settings_doc or not settings_doc.get("no_due_access_enabled"):
            return

        auto_disable_at = settings_doc.get("auto_disable_at")
        if auto_disable_at:
            if auto_disable_at.tzinfo is None:
                auto_disable_at = auto_disable_at.replace(tzinfo=timezone.utc)
            if now_utc >= auto_disable_at:
                portal_settings.update_one(
                    {"_id": "global_config"},
                    {"$set": {"no_due_access_enabled": False}}
                )
                logger.info("Auto-disabled No Due Access via maintenance cycle.")
                try:
                    from .events import notify_all_students, notify_faculty
                    notify_all_students("no_due_access_toggled", {
                        "enabled": False,
                        "auto_disable_str": "",
                    })
                    notify_faculty("no_due_access_toggled", {
                        "enabled": False,
                        "auto_disable_str": "",
                    })
                except Exception as be:
                    logger.warning("[WS] Failed to broadcast auto-disable: %s", be)
    except PyMongoError as exc:
        logger.error("_check_auto_disable_access failed: %s", exc)


def _delete_cloudinary(public_id):
    """Best-effort delete; resource type isn't tracked so try raw then image."""
    if not public_id or public_id == "7.5_scheme":
        return
    try:
        import cloudinary.uploader
        for rtype in ("raw", "image"):
            try:
                cloudinary.uploader.destroy(public_id, resource_type=rtype)
            except Exception:
                continue
    except Exception as exc:  # cloudinary import / network failure
        logger.warning("Cloudinary delete failed for %s: %s", public_id, exc)


def _expire_stale_pending():
    """
    Revert PENDING requests older than PENDING_TTL back to NOT_SENT, decrement
    their attempts, and clean up any HOSTEL receipts. Bounded per cycle.
    """
    now = datetime.now()
    cutoff = now - PENDING_TTL
    reverted = 0
    try:
        cursor = list(no_due_col.find(
            {"status": "PENDING", "created_at": {"$lte": cutoff}},
            {"office": 1, "student_id": 1, "attempts_used": 1, "cloudinary_public_id": 1},
        ).limit(1000))
    except PyMongoError as exc:
        logger.error("_expire_stale_pending query failed: %s", exc)
        return 0

    deletes = 0
    for req in cursor:
        try:
            if req.get("office") == "HOSTEL" and deletes < MAX_CLOUDINARY_DELETES_PER_CYCLE:
                _delete_cloudinary(req.get("cloudinary_public_id"))
                deletes += 1

            current = req.get("attempts_used", 1)
            new_attempts = max(0, current - 1)
            no_due_col.update_one(
                {"_id": req["_id"], "status": "PENDING"},  # re-check: don't stomp a just-approved req
                {"$set": {
                    "status": "NOT_SENT",
                    "attempts_used": new_attempts,
                    "receipt_url": None,
                    "cloudinary_public_id": None,
                    "updated_at": now,
                }},
            )
            reverted += 1

            # Broadcast real-time update to student and office
            try:
                from .events import notify_student, notify_office
                notify_student(req.get("student_id"), "request_status_updated", {
                    "office": req.get("office"),
                    "status": "NOT_SENT",
                    "attempts_used": new_attempts,
                    "attempts_remaining": max(0, 2 - new_attempts),
                })
                notify_office(req.get("office"), "request_expired", {
                    "request_id": str(req["_id"]),
                })
            except Exception as be:
                logger.warning("[WS] Failed to broadcast pending timeout: %s", be)

        except PyMongoError as exc:
            logger.error("Failed to revert pending req %s: %s", req.get("_id"), exc)
    return reverted


def run_maintenance_cycle(lock_ttl_seconds=50):
    """
    Perform one maintenance cycle under a distributed Mongo lock so only one
    worker runs it at a time. Returns a small summary dict (or None if skipped).
    Never raises.
    """
    now = datetime.now(timezone.utc)
    lock_expiry = now + timedelta(seconds=lock_ttl_seconds)
    try:
        # Acquire lock: succeeds only if no live lock exists (or it expired).
        lock = portal_settings.find_one_and_update(
            {
                "_id": "maintenance_lock",
                "$or": [
                    {"locked_until": {"$exists": False}},
                    {"locked_until": {"$lte": now}},
                ],
            },
            {"$set": {"locked_until": lock_expiry, "locked_at": now}},
            upsert=True,
            return_document=True,
        )
        if not lock:
            return None  # another worker holds the lock
    except PyMongoError:
        # Race on the upsert unique _id => someone else just took it. Fine.
        return None

    try:
        fast_cooldown_reset()
        _check_auto_disable_access()
        reverted = _expire_stale_pending()
        if reverted:
            logger.info("Maintenance cycle: reverted %d stale PENDING requests", reverted)
        return {"reverted_pending": reverted}
    except Exception as exc:  # defensive: never let the scheduler thread die
        logger.exception("run_maintenance_cycle error: %s", exc)
        return {"error": str(exc)}
    finally:
        try:
            portal_settings.update_one(
                {"_id": "maintenance_lock"},
                {"$set": {"locked_until": now}},  # release early
            )
        except PyMongoError:
            pass
