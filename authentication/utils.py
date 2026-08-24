"""
Request-path helpers.

The heavy cleanup that used to run inline on every dashboard load (expiring
stale PENDING requests + blocking Cloudinary deletes in a loop) now lives in
`maintenance.py` and runs in the background scheduler / `run_maintenance`
command. On the request path we only do the cheap, single-query cooldown reset
so students never see a stale cooldown.
"""
import logging

import cloudinary.uploader

from .maintenance import fast_cooldown_reset

logger = logging.getLogger("nodue")


def save_receipt(file):
    """Upload a receipt to Cloudinary and return its secure URL."""
    result = cloudinary.uploader.upload(
        file,
        folder="no_due_receipts",
        resource_type="auto",   # PDF + image both
    )
    return result["secure_url"]


def reset_expired_no_dues(no_due_col=None):
    """
    Kept for backward compatibility with existing view imports.

    Now a thin, cheap wrapper: it only clears elapsed 24h cooldowns (one bulk
    update). Throttled to max once per 30s per worker on request paths to
    prevent database round-trip overhead on rapid page loads.
    """
    fast_cooldown_reset(throttle_seconds=30)


def record_student_no_due_completion(student_id):
    """
    Checks if all required offices for a student are currently APPROVED.
    If all required offices are approved, immediately records a No Due completion log
    with the student's current Year and Semester in `promotion_logs`.

    Guarantees:
    - Does NOT wait for student promotion.
    - Saves the student's current Year and Semester at the time of completion.
    - Idempotent: Prevents duplicate logs for the same student, year, and semester.
    - Accurately respects Day Scholar (3 offices) vs Hosteller (4 offices).
    - Returns True if completed and log exists/created, False otherwise.
    """
    from .mongo import students_col, no_due_col, promotion_logs
    from bson import ObjectId
    from bson.errors import InvalidId
    from datetime import datetime

    if not student_id:
        return False

    try:
        obj_id = ObjectId(student_id) if not isinstance(student_id, ObjectId) else student_id
    except (InvalidId, TypeError):
        return False

    try:
        student = students_col.find_one({"_id": obj_id})
        if not student:
            return False

        student_type = student.get("student_type", "Hosteller")
        if student_type == "Day Scholar":
            required_offices = ["LIBRARY", "COLLEGE", "DEPARTMENT"]
        else:
            required_offices = ["LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"]

        # Check if all required offices are APPROVED
        approved_count = no_due_col.count_documents({
            "student_id": obj_id,
            "office": {"$in": required_offices},
            "status": "APPROVED"
        })

        if approved_count != len(required_offices):
            return False

        current_year = int(student.get("year", 1))
        current_sem = int(student.get("semester", 1))
        now = datetime.now()

        # Idempotent check: prevent duplicate logs for the same student, year, and semester
        existing = promotion_logs.find_one({
            "student_id": obj_id,
            "$or": [
                {"previous_semester": current_sem, "previous_year": current_year},
                {"semester": current_sem, "year": current_year},
            ]
        })

        if not existing:
            promotion_logs.insert_one({
                "student_id": obj_id,
                "previous_semester": current_sem,
                "previous_year": current_year,
                "semester": current_sem,
                "year": current_year,
                "student_type": student_type,
                "completion_time": now,
                "promotion_time": now,
                "no_due_cleared": True,
                "status": "Completed",
            })
            logger.info("Recorded No Due Completion Log for student %s: Year %s - Semester %s Completed",
                        obj_id, current_year, current_sem)
        else:
            # Ensure no_due_cleared is True on existing log if not already set
            if not existing.get("no_due_cleared"):
                promotion_logs.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {
                        "no_due_cleared": True,
                        "status": "Completed",
                        "completion_time": existing.get("completion_time") or now,
                        "updated_at": now
                    }}
                )

        return True
    except Exception as exc:
        logger.error("Error recording no due completion log for student %s: %s", student_id, exc)
        return False

