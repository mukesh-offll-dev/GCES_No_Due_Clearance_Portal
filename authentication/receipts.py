"""
🧾 Hostel fee receipts in Cloudinary — when they are deleted.

    Rejected by the Hostel Office      → deleted at once
    Not answered for 2 days (expired)  → deleted by the maintenance cycle
    Duplicate upload (double submit)   → deleted at once
    APPROVED                           → kept while the student is in that semester; deleted after
                                         the student is PROMOTED and that promotion can no longer
                                         be undone (a promoted student sent a new request, or a
                                         newer promotion exists). If the promotion is undone, the
                                         receipt is kept (the request points to it again).

Anything that can't be deleted right now goes into the ``receipt_cleanup`` queue
(one doc per Cloudinary public_id) which ``process_queue()`` works through every
maintenance cycle. Failed deletes are retried with a growing delay.

Safety: a file is NEVER deleted while a current no-due request or an archived
student still points to it.
"""
import logging
from datetime import datetime, timedelta, timezone

from pymongo.errors import PyMongoError

from .mongo import db, no_due_col

logger = logging.getLogger("nodue.maintenance")
audit_logger = logging.getLogger("nodue.audit")

receipt_cleanup = db["receipt_cleanup"]
MAX_PER_CYCLE = 200
MAX_ATTEMPTS = 12                      # ~ a day of retries with the back-off below
_NO_FILE = {None, "", "7.5_scheme"}


def destroy(public_id):
    """Delete one file from Cloudinary. True when it is gone (deleted now or already missing)."""
    if public_id in _NO_FILE:
        return True
    try:
        import cloudinary.uploader
    except Exception as exc:
        logger.warning("Cloudinary unavailable: %s", exc)
        return False
    ok = True
    for rtype in ("image", "raw"):          # PDFs may be stored as either; the type isn't recorded
        try:
            result = (cloudinary.uploader.destroy(public_id, resource_type=rtype, invalidate=True) or {}).get("result")
            if result not in ("ok", "not found"):
                ok = False
        except Exception as exc:
            logger.warning("Cloudinary delete failed for %s (%s): %s", public_id, rtype, exc)
            ok = False
    return ok


def queue(public_id, reason, run_id=None):
    if public_id in _NO_FILE:
        return
    now = datetime.now(timezone.utc)
    fields = {"reason": reason, "next_try_at": now}
    if run_id is not None:
        fields["run_id"] = run_id
    try:
        receipt_cleanup.update_one({"_id": public_id},
                                   {"$set": fields, "$setOnInsert": {"queued_at": now, "attempts": 0}},
                                   upsert=True)
    except PyMongoError as exc:
        logger.error("Could not queue receipt %s for deletion: %s", public_id, exc)


def delete_or_queue(public_id, reason):
    """Delete now; if Cloudinary fails, keep it in the queue so it is retried later."""
    if public_id in _NO_FILE:
        return
    if not destroy(public_id):
        queue(public_id, reason)


def queue_promoted(items, run_id):
    """After a successful promotion: queue every receipt the promoted students had (Hostel)."""
    for it in items:
        for due in it.get("dues") or []:
            if due.get("office") == "HOSTEL" and due.get("cloudinary_public_id"):
                queue(due["cloudinary_public_id"], "promoted", run_id=run_id)


def _still_used(public_id):
    """True when a current request or an archived student still points to this file."""
    if no_due_col.find_one({"cloudinary_public_id": public_id}, {"_id": 1}):
        return True
    return bool(db["archived_students"].find_one({"no_dues.cloudinary_public_id": public_id}, {"_id": 1}))


def _promotion_state(run_id, cache):
    """'wait' (undo still possible), 'drop' (undone/failed → keep the file) or 'delete'."""
    if run_id in cache:
        return cache[run_id]
    from .promotion import last_run, promotion_runs, undo_blockers
    run = promotion_runs.find_one({"_id": run_id})
    if not run:
        state = "delete"                                 # journal gone; only the safety check protects it
    elif run.get("status") in ("in_progress", "interrupted"):
        state = "wait"
    elif run.get("status") != "completed":
        state = "drop"                                   # rolled back / auto-undone / failed: receipts restored
    else:
        latest = last_run()
        undo_possible = latest is not None and latest["_id"] == run["_id"] and undo_blockers(run) == 0
        state = "wait" if undo_possible else "delete"
    cache[run_id] = state
    return state


def _backfill_once():
    """Queue receipts from promotions made before this cleanup existed (once per database)."""
    settings_col = db["portal_settings"]
    if settings_col.find_one({"_id": "receipt_backfill", "done": True}, {"_id": 1}):
        return 0
    n = 0
    from .promotion import promotion_run_items, promotion_runs
    done_runs = promotion_runs.distinct("_id", {"status": "completed", "mode": {"$ne": "fix"}})
    for it in promotion_run_items.find({"run_id": {"$in": done_runs}, "dues.office": "HOSTEL"},
                                       {"run_id": 1, "dues": 1}):
        for due in it.get("dues") or []:
            if due.get("office") == "HOSTEL" and due.get("cloudinary_public_id"):
                queue(due["cloudinary_public_id"], "promoted", run_id=it["run_id"])
                n += 1
    settings_col.update_one({"_id": "receipt_backfill"}, {"$set": {"done": True, "queued": n,
                                                                    "at": datetime.now(timezone.utc)}}, upsert=True)
    if n:
        logger.info("Receipt cleanup: queued %d receipts from earlier promotions", n)
    return n


def process_queue(limit=MAX_PER_CYCLE):
    """Work through the queue. Returns {'deleted', 'kept', 'waiting', 'failed'}."""
    stats = {"deleted": 0, "kept": 0, "waiting": 0, "failed": 0}
    try:
        _backfill_once()
    except Exception as exc:
        logger.warning("Receipt backfill skipped: %s", exc)
    now = datetime.now(timezone.utc)
    cache = {}
    try:
        entries = list(receipt_cleanup.find({"next_try_at": {"$lte": now}}).sort("next_try_at", 1).limit(limit))
    except PyMongoError as exc:
        logger.error("Receipt queue read failed: %s", exc)
        return stats
    for e in entries:
        pid = e["_id"]
        try:
            if e.get("run_id") is not None:
                state = _promotion_state(e["run_id"], cache)
                if state == "wait":
                    receipt_cleanup.update_one({"_id": pid}, {"$set": {"next_try_at": now + timedelta(minutes=30)}})
                    stats["waiting"] += 1
                    continue
                if state == "drop":
                    receipt_cleanup.delete_one({"_id": pid})
                    stats["kept"] += 1
                    continue
            if _still_used(pid):
                receipt_cleanup.delete_one({"_id": pid})
                stats["kept"] += 1
                continue
            if destroy(pid):
                receipt_cleanup.delete_one({"_id": pid})
                audit_logger.info("RECEIPT deleted public_id=%s reason=%s", pid, e.get("reason"))
                stats["deleted"] += 1
            else:
                attempts = int(e.get("attempts", 0)) + 1
                delay = timedelta(minutes=min(240, 5 * 2 ** min(attempts, 6)))
                receipt_cleanup.update_one({"_id": pid}, {"$set": {"attempts": attempts, "next_try_at": now + delay}})
                if attempts >= MAX_ATTEMPTS:
                    logger.error("Receipt %s still not deleted after %d tries", pid, attempts)
                stats["failed"] += 1
        except PyMongoError as exc:
            logger.error("Receipt cleanup failed for %s: %s", pid, exc)
    return stats
