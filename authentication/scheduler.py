import threading
import time
from datetime import datetime, timedelta
from .mongo import students_col, no_due_col, promotion_logs


def check_and_promote_students():
    """
    Checks all non-graduated students.
    - Hosteller  : all 4 offices (LIBRARY, HOSTEL, COLLEGE, DEPARTMENT) must be APPROVED
    - Day Scholar: 3  offices (LIBRARY, COLLEGE, DEPARTMENT) must be APPROVED

    After PROMOTION_COOLDOWN_MINUTES since last approval, promotes semester/year,
    resets ALL no-due records to NOT_SENT, and writes a promotion log.

    After GRADUATION_DELETE_COOLDOWN_MINUTES past graduation, deletes student + all records.
    """
    from django.conf import settings
    cooldown_minutes     = getattr(settings, 'PROMOTION_COOLDOWN_MINUTES',        2)
    delete_cooldown_mins = getattr(settings, 'GRADUATION_DELETE_COOLDOWN_MINUTES', 3)

    now = datetime.now()

    # ── SEMESTER PROGRESSION MAP ──────────────────────────────────────────────
    progression = {
        1: (2, 1),
        2: (3, 2),
        3: (4, 2),
        4: (5, 3),
        5: (6, 3),
        6: (7, 4),
        7: (8, 4),
        8: ("Graduated", 4),
    }

    # ── PROMOTION LOOP ────────────────────────────────────────────────────────
    students = list(students_col.find({"semester": {"$ne": "Graduated"}}))

    for student in students:
        student_id   = student["_id"]
        current_sem  = student.get("semester")
        current_year = student.get("year")

        # Only promote integer semesters 1–8
        try:
            current_sem_int = int(current_sem)
        except (ValueError, TypeError):
            continue

        # ── Determine required offices for this student ───────────────────────
        student_type = (student.get("student_type") or "Hosteller").strip()
        if student_type == "Day Scholar":
            required_offices = ["LIBRARY", "COLLEGE", "DEPARTMENT"]
            required_count   = 3
        else:
            required_offices = ["LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"]
            required_count   = 4

        # ── Fetch APPROVED records for the required offices ───────────────────
        approved_records = list(no_due_col.find({
            "student_id": student_id,
            "office":     {"$in": required_offices},
            "status":     "APPROVED"
        }))

        # Count UNIQUE approved offices (duplicate records won't inflate count)
        approved_offices_set = set(r["office"] for r in approved_records)

        # Check all required offices are present
        if approved_offices_set < set(required_offices):
            continue   # at least one required office not yet approved

        # ── Find the latest approval timestamp across required offices ─────────
        completion_time = None
        for office in required_offices:
            office_records = [r for r in approved_records if r["office"] == office]
            if not office_records:
                break   # safety: required office missing → skip
            t = max(
                (r.get("updated_at") or r.get("created_at") or now)
                for r in office_records
            )
            if completion_time is None or t > completion_time:
                completion_time = t
        else:
            pass  # for-loop completed without break → all offices confirmed

        if completion_time is None:
            completion_time = now

        if now - completion_time < timedelta(minutes=cooldown_minutes):
            continue   # cooldown not yet passed

        # ── Determine next semester / year ────────────────────────────────────
        if current_sem_int not in progression:
            continue
        next_sem, next_year = progression[current_sem_int]

        # ── 1. Update student document ────────────────────────────────────────
        update_fields = {"semester": next_sem, "year": next_year}
        if next_sem == "Graduated":
            update_fields["graduated_at"] = now

        students_col.update_one({"_id": student_id}, {"$set": update_fields})

        # ── 2. Reset ALL no_due records for this student ──────────────────────
        #    Resets every office (not just required set) so stale HOSTEL records
        #    are also cleared when a student is changed to Day Scholar.
        if next_sem != "Graduated":
            no_due_col.update_many(
                {"student_id": student_id},
                {"$set": {
                    "status":               "NOT_SENT",
                    "receipt_url":          None,
                    "cloudinary_public_id": None,
                    "reject_reason":        None,
                    "last_payment_id":      None,
                    "updated_at":           now,
                }}
            )

        # ── 3. Write promotion log ────────────────────────────────────────────
        promotion_logs.insert_one({
            "student_id":        student_id,
            "previous_semester": current_sem_int,
            "previous_year":     current_year,
            "new_semester":      next_sem,
            "new_year":          next_year,
            "student_type":      student_type,
            "completion_time":   completion_time,
            "promotion_time":    now,
        })

    # ── GRADUATION DELETION LOOP ──────────────────────────────────────────────
    graduated = list(students_col.find({
        "semester":     "Graduated",
        "graduated_at": {"$exists": True}
    }))

    for g in graduated:
        g_id    = g["_id"]
        grad_at = g.get("graduated_at")
        if not grad_at:
            continue

        if now - grad_at < timedelta(minutes=delete_cooldown_mins):
            continue   # deletion cooldown not yet passed

        # ── Delete student + all related data ────────────────────────────────
        students_col.delete_one({"_id": g_id})
        no_due_col.delete_many({"student_id": g_id})
        promotion_logs.delete_many({"student_id": g_id})


# ── SCHEDULER THREAD ──────────────────────────────────────────────────────────

def scheduler_loop():
    while True:
        try:
            check_and_promote_students()
        except Exception:
            pass
        time.sleep(30)   # run every 30 seconds


def start_scheduler():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
