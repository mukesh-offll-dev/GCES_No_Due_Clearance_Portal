"""
MongoDB index management.

There is no ORM schema, so indexes are declared here and created idempotently
at startup (see apps.ready) and via the `ensure_indexes` management command.

Every index below is justified by an actual query in views.py / utils.py —
we do NOT add speculative indexes.
"""
import logging
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure, PyMongoError

from .mongo import (
    students_col,
    no_due_col,
    promotion_logs,
    institution_logs,
    departments_col,
)

logger = logging.getLogger("nodue.mongo")


def ensure_indexes():
    """
    Create all required indexes. Safe to call repeatedly — createIndex is a
    no-op when the index already exists. Never raises: a failure here must not
    stop the app from booting (it just means queries stay unindexed).
    """
    created = []
    try:
        # ── departments ─────────────────────────────────────────────
        _safe(created, departments_col, [("code", ASCENDING)],
              name="dept_code_unique", unique=True)
        _safe(created, departments_col, [("is_active", ASCENDING)],
              name="dept_is_active")
        _safe(created, departments_col, [("name", ASCENDING)],
              name="dept_name")

        # ── students ────────────────────────────────────────────────
        # student_login: {reg_no, dob};  add_student/import dup check: {reg_no}|{roll_no}
        # reg_no must be unique (login key + dup guard).
        _safe(created, students_col, [("reg_no", ASCENDING)],
              name="reg_no_unique", unique=True)
        _safe(created, students_col, [("roll_no", ASCENDING)],
              name="roll_no_unique", unique=True)
        # dashboards / reports / promotion filter heavily on branch + year + semester.
        _safe(created, students_col, [("branch", ASCENDING), ("year", ASCENDING)],
              name="branch_year")
        _safe(created, students_col, [("semester", ASCENDING)], name="semester")
        _safe(created, students_col, [("year", ASCENDING)], name="year")

        # ── no_due_requests ─────────────────────────────────────────
        # Every student action queries {student_id, office}; make it UNIQUE so
        # concurrent double-submits cannot create two docs for the same office.
        _safe(created, no_due_col, [("student_id", ASCENDING), ("office", ASCENDING)],
              name="student_office_unique", unique=True)
        # Office dashboards: {office, status} then $lookup by student_id.
        _safe(created, no_due_col, [("office", ASCENDING), ("status", ASCENDING)],
              name="office_status")
        # Maintenance sweep: expire PENDING by created_at; clear cooldowns.
        _safe(created, no_due_col, [("status", ASCENDING), ("created_at", ASCENDING)],
              name="status_created_at")
        _safe(created, no_due_col, [("cooldown_expiry", ASCENDING)],
              name="cooldown_expiry", sparse=True)
        # student_dashboard / certificate lookups by student_id alone.
        _safe(created, no_due_col, [("student_id", ASCENDING)], name="student_id")

        # ── promotion_logs ──────────────────────────────────────────
        _safe(created, promotion_logs,
              [("student_id", ASCENDING), ("promotion_time", DESCENDING)],
              name="student_promotion_time")
        _safe(created, promotion_logs,
              [("student_id", ASCENDING), ("previous_semester", ASCENDING), ("previous_year", ASCENDING)],
              name="student_term_log")

        # ── institution_logs ────────────────────────────────────────
        _safe(created, institution_logs, [("login_time", DESCENDING)],
              name="login_time")

        if created:
            logger.info("MongoDB indexes ensured: %s", ", ".join(created))
        else:
            logger.info("MongoDB indexes already present.")
    except PyMongoError as exc:
        logger.error("ensure_indexes failed (app continues without new indexes): %s", exc)
    return created


def _safe(created, col, keys, **opts):
    """Create one index, tolerating the 'already exists / duplicate data' cases."""
    try:
        col.create_index(keys, background=True, **opts)
        created.append("%s.%s" % (col.name, opts.get("name", keys)))
    except OperationFailure as exc:
        # Common, non-fatal: a unique index cannot be built because the
        # collection already holds duplicate values. Log so it can be cleaned
        # up manually — do NOT crash the boot.
        logger.warning(
            "Could not create index %s on %s: %s",
            opts.get("name", keys), col.name, exc,
        )
    except PyMongoError as exc:
        logger.warning("Index creation error on %s: %s", col.name, exc)
