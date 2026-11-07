# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Django-based "No Due" clearance portal for Government College of Engineering, Srirangam (GCES). Students request clearance ("no dues") from institutional offices (Library, Hostel, College, Department); office staff approve/reject; faculty manage the student roster and semester promotions.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run development server (DEBUG must be True in .env for local HTTP)
python manage.py runserver

# Production (see Procfile) — gthread workers, auto-scaled, ~400 slots
gunicorn -c gunicorn.conf.py nodue_portal.wsgi

# Collect static files (WhiteNoise, required before deploy)
python manage.py collectstatic --noinput

# Build MongoDB indexes (idempotent; also auto-run at server boot)
python manage.py ensure_indexes

# Maintenance cycle (also runs in a background thread inside each worker)
python manage.py run_maintenance         # once, for a systemd timer/cron
python manage.py run_maintenance --loop  # standalone daemon
```

See `PRODUCTION.md` for the full deployment guide (systemd units, env tuning,
load testing). Worker config lives in `gunicorn.conf.py`.

Tests: `MONGO_URI=mongodb://localhost:27017 python manage.py test authentication` (needs a local MongoDB; never point tests at Atlas). Tests use `SimpleTestCase` because there is no SQL database. No linter is configured. There is no `migrate` step — the project has no Django ORM tables.

## Critical Architecture Note: MongoDB Only (no SQL)

This project uses **MongoDB (via pymongo) as the real application database**, NOT the Django ORM.

- There is **no SQL database**: `DATABASES = {}` in `settings.py`, and `django.contrib.admin/auth/contenttypes/sessions` are not installed. Login sessions use a custom MongoDB session engine (`authentication/mongo_sessions.py`, `SESSION_ENGINE`), stored in the `django_sessions` collection with a TTL index on `expire_date` (built in `db_indexes.py`). `django.contrib.auth.hashers`/`password_validation` are still used as plain libraries for student passwords.
- **MongoDB** (`authentication/mongo.py`) holds all business data. Connected via `MONGO_URI` env var to database `no_dues_portal`. `authentication/models.py` is intentionally empty — do not add Django models for app data.

MongoDB collections (all accessed as module-level globals imported from `.mongo`):
- `students` (`students_col`) — student records, keyed by `_id` (ObjectId). Fields include `reg_no`, `roll_no`, `name`, `dob`, `phone`, `branch`, `year`, `semester`, `student_type` (`"Hosteller"` | `"Day Scholar"`), `is_75_scheme`.
- `no_due_requests` (`no_due_col`) — one doc per (student, office). Status flow: `NOT_SENT` → `PENDING` → `APPROVED`/`REJECTED`. Tracks `attempts_used`, `cooldown_expiry`, receipts.
- `institution_logs` (`institution_logs`) — office/department login audit.
- `promotion_logs` (`promotion_logs`) — semester promotion history per student.
- `portal_settings` (`portal_settings`) — single doc `_id: "global_config"` holding the global no-due access toggle and promotion lock.

Because there's no ORM schema, `.find_one().get("field", default)` defensive access is the norm. Match that style.

## Authentication & Authorization

There is **no Django `User` model auth**. Two custom mechanisms, both session-based:

1. **Office/Faculty/Department logins** (`index` view → `accounts.authenticate`): usernames + passwords live in MongoDB `institution_accounts` (`_id` = `LIBRARY`/`COLLEGE`/`HOSTEL`/`FACULTY`/`DEPARTMENT:<CODE>`/`PROMOTION`; see `authentication/accounts.py`). Each doc has `password_hash` (checked at login) and `password_enc` (Fernet, key derived from `SECRET_KEY`, only so faculty can view it). Missing logins are created from `DEFAULT_LOGINS` / `DEFAULT_PROMOTION_PASSWORD` at start-up and lazily on first login (`ensure_default_accounts`, never overwrites). Faculty view and change every password on Promotion & Settings → **Password Management** tab (`update_account_password`). The old guessable `<code>admin` / `<code>@123` department fallback is gone; inactive departments cannot log in. Department code rename/delete keeps `institution_accounts` in sync. On success the session is cycled and `role` (+ `department`) set.
2. **Student login** (`student_login`): `reg_no` + password (`password_hash`, Django hashers). First login uses the Register Number as a temporary password and goes to `student_profile_setup` (name, roll no, DOB, phone, **email**, new password). Sets `role="STUDENT"`, `student_id` and `pw_version`.
   - **Forgot password** (`password_reset.py`, views `student_forgot_password` → `student_reset_password`): Register No → 6-digit code emailed via the **EmailJS REST API** to `students.email`; only an HMAC of the code is stored in `password_resets` (TTL on `purge_at`). Code valid `PASSWORD_RESET_CODE_MINUTES` (10), 5 wrong tries, resend after 60 s, max 5 codes/hour. A reset bumps `students.password_version`; `institution_login_required` logs out sessions whose `pw_version` differs. Students add/change their email from the dashboard (`student_update_email`, needs current password); faculty can fix it in Edit Student. Tests mock `password_reset.send_email` — never send real mail from tests.

`@institution_login_required` (`authentication/decorators.py`) guards protected views: it re-validates a STUDENT's `student_id` against MongoDB on every request (not just trusting the cookie). **Individual views additionally check `request.session.get("role")` matches the expected role** — this per-view role check is the authorization layer; keep it when adding views.

Faculty promotion/toggle actions require a second gate: `request.session["promotion_unlocked"]`, set by a separate password prompt (`faculty_promotion_page`). That password is the `PROMOTION` doc in `institution_accounts`, changeable on the Password Management tab.

**Wrong-password lockout** (`login_guard.py`, collection `login_attempts`, TTL): 5 wrong passwords within 15 min locks that account for 15 min — keys `student:<reg_no>`, `inst:<office>:<dept>:<username>`, and one shared `promotion` key. Counted per account, not per IP (campus NAT). A student's password reset clears their lock.

App startup (`apps.ready`) pre-imports pymongo and app modules on the main thread before starting the index/scheduler threads (concurrent imports tripped Python's import-lock deadlock detection), and one-off `manage.py` commands other than `runserver` never start those threads.

The `_ROLE_REDIRECT` dict in `views.py` maps roles to their dashboard URL names.

## Key Business Logic (all in `authentication/views.py`)

- **Required offices depend on `student_type`**: Day Scholars need LIBRARY/COLLEGE/DEPARTMENT (3); Hostellers add HOSTEL (4). This branching recurs across `student_dashboard`, `promote_students`, and the report/status aggregation pipelines — update all of them together.
- **Attempt/cooldown system**: `attempts_used` counts office **rejections** (max 2) — sending a request or a request expiring never uses an attempt. The 2nd rejection sets a 24h `cooldown_expiry`; when it ends, attempts reset to 0 (cleared inline by `_clear_elapsed_cooldown` in send/retry/dashboard and by the sweep). Docs written under this rule have `attempts_model: 2`; older docs counted sends, and reject/expiry code handles both. `reset_expired_no_dues()` (in `utils.py`) is still called at the top of most dashboards but is now a **thin, cheap wrapper** that only clears elapsed cooldowns in one bulk update (`fast_cooldown_reset`). The heavy work — expiring stale `PENDING` requests (older than 2 days, `PENDING_TTL` → back to `NOT_SENT` so the student can resend; attempts unchanged) and deleting orphaned Cloudinary receipts — moved to `maintenance.py` (`run_maintenance_cycle`), run by a background scheduler thread (`scheduler.py`, started in `apps.ready`) and the `run_maintenance` command. A MongoDB lock (`portal_settings._id="maintenance_lock"`) ensures only one worker runs a cycle at a time. Cooldown datetimes may be naive or tz-aware; code normalizes to UTC before comparing.
- **Indexes & concurrency**: MongoDB indexes are declared in `db_indexes.py` and built at boot / via `ensure_indexes`. There are **unique** indexes on `students.reg_no`, `students.roll_no`, and `no_due_requests.(student_id, office)` — the last one is the DB-level guard against duplicate no-due docs, so student request views catch `DuplicateKeyError` on upsert races. `ExceptionHandlingMiddleware`/`RequestLogMiddleware` (in `middleware.py`) provide the global error safety net (DB errors → 503, else → 500) and structured access logs. Logging config is in `settings.py` (`nodue`, `nodue.access`, `nodue.error`, `nodue.audit`, `nodue.mongo` loggers).
- **Global no-due access toggle** (`check_no_due_access_status`): faculty enable/disable the whole request process with an `auto_disable_at` timestamp; the check lazily auto-disables when that time passes. All student request views short-circuit if this is off.
- **One semester per Department + Year** (`promotion.check_cohort_semester`): Year y = Semesters 2y-1/2y and every student of a Department + Year must share one semester. Enforced in Add Student, Bulk Add, Edit Student, the student's own profile update, Excel import and promotion; roster changes are refused while a promotion lock is held.
- **Semester promotion** lives in `authentication/promotion.py` (`run_promotion`; `promote_students` only parses the form and broadcasts). Full = Semesters 1–7 (processed 7→1); semester-wise = one `from_sem` across all departments (`promotion_mode=semester` never falls back to full). Rules are simulated **per department**: no batch merge into a year that already has non-moving students, and no Department + Year may end up with two semesters; Semester 8 must be empty for full promotion and for 2→3/4→5/6→7. The page shows `promotion_checks()` results (single source of truth) and posts `counts_snapshot` (Department × Semester counts); a mismatch aborts. Year is derived: `(semester+1)//2`. Every run is journalled first (`promotion_runs`, `promotion_run_items`: old/new semester+year, no-due docs, history row) and applied in one transaction when MongoDB supports it (Atlas); on a standalone mongod a failure replays the journal backwards. `rollback_last_run` / `/faculty/promotion/undo/` undoes only the most recent run (type UNDO) and is refused once any of its students sent a request for the new semester (`undo_blockers`). Runs left `in_progress`/`interrupted` by a killed process are undone automatically by `recover_interrupted_runs()` (maintenance cycle + start of every promotion; only when no live lock). After each run, if a previously consistent class now has two semesters, the run auto-undoes. Semester-wise promotion can be limited to one department (`promote_branch`; Sem 8 rule then per department). Mixed classes are repaired with `fix_cohort` (`/faculty/promotion/fix-class/`, journalled, undoable). Semester 8 removal and faculty Delete copy students to `archived_students`; restore via `/faculty/promotion/restore/` (skips Register/Roll conflicts and cohort-rule violations). Roster writes re-check the class after writing (`verify_cohort_after_write`) and reverse themselves if it broke the rule or a promotion started; promotion waits `WRITE_GRACE_SECONDS` after taking the lock and extends it per batch (heartbeat). Lock: `promotion_in_progress` + `promotion_lock_until` (10 min) in `portal_settings`.
- **7.5 scheme students** bypass hostel receipt upload.

## File Uploads

Hostel fee receipts upload to **Cloudinary** (folder `no_dues/hostel`), storing `receipt_url` + `cloudinary_public_id` on the HOSTEL no-due doc. All deletes go through `authentication/receipts.py` (`destroy()` tries `image` and `raw` since the type isn't tracked; "not found" counts as gone):
- Rejected → deleted at once (`delete_or_queue`); expired (2 days) → deleted by the maintenance cycle; duplicate upload from a submit race → deleted at once.
- **Approved** → kept while the student is in that semester. `run_promotion` queues the promoted students' receipts (`queue_promoted`) and `process_queue()` (every maintenance cycle) deletes them only when that run can no longer be undone (not the latest undoable run, or `undo_blockers > 0`); an undone/failed run drops the entry and keeps the file.
- Queue = `receipt_cleanup` collection (one doc per public_id). Failed deletes are retried with back-off. A file is never deleted while a current no-due doc or an `archived_students.no_dues` entry still references it. Receipts from journalled promotions made before this existed are back-filled once (`portal_settings._id="receipt_backfill"`). The `media/` directory and `save_receipt()` in `utils.py` are a legacy local-storage path still referenced by `send_no_due_request` for non-hostel — prefer the Cloudinary flow.

## Reports

`office_report_pdf_view` generates styled PDFs with ReportLab (`NumberedCanvas` adds page numbers). `office_report_preview_api` / `office_student_status_api` return paginated JSON using MongoDB `$facet` aggregation. `_get_report_students_pipeline` builds the shared aggregation pipeline (student → no_due lookup → computed `office_status`) for the three report types (`year`, `branch`, `year_branch`).

## Configuration

Environment variables (loaded from `.env` via python-dotenv in `settings.py`):
- `MONGO_URI` — MongoDB connection string (required; app data is unavailable without it)
- `SECRET_KEY`, `DEBUG` (`"True"`/`"False"`)
- `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`
- `APP_MAIL_ENABLED`, `EMAILJS_SERVICE_ID`, `EMAILJS_TEMPLATE_ID`, `EMAILJS_PUBLIC_KEY`, `EMAILJS_PRIVATE_KEY` — student password-reset email (off unless all are set). The EmailJS account must allow API calls from non-browser apps; the template's To Email is `{{to_email}}` and the code is `{{passcode}}`.

`DEBUG` controls cookie security flags (`SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` off in dev). Deployed on Render (`CSRF_TRUSTED_ORIGINS`, Procfile). Sessions last 2h with sliding expiry.

## Conventions

- All routing is flat in `authentication/urls.py`; views are all function-based in one large `views.py`. URL names (e.g. `student_dashboard`, `faculty_promotion`) are used for redirects throughout — keep them stable.
- Comments frequently mix English and Tamil (transliterated) and use emoji section markers. This is existing style, not a mistake.
