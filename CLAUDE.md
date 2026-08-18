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

# Production (see Procfile)
gunicorn nodue_portal.wsgi

# Collect static files (WhiteNoise, required before deploy)
python manage.py collectstatic --noinput
```

There is no test suite (`authentication/tests.py` is empty) and no linter configured. `python manage.py migrate` only affects the SQLite session/admin store — application data lives in MongoDB (see below).

## Critical Architecture Note: Two Databases

This project uses **MongoDB (via pymongo) as the real application database**, NOT the Django ORM.

- **SQLite** (`db.sqlite3`, configured in `settings.py` `DATABASES`) exists *only* because Django requires it — it backs sessions, admin, and auth tables. Django migrations apply here.
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

1. **Office/Faculty/Department logins** (`index` view): credentials are **hardcoded in `authentication/institution_users.py`**. On success, `request.session["role"]` is set to one of `LIBRARY`, `HOSTEL`, `COLLEGE`, `FACULTY`, `DEPARTMENT`. Department logins also set `request.session["department"]`.
2. **Student login** (`student_login`): matches `reg_no` + `dob` against MongoDB; sets `role="STUDENT"` and `student_id`.

`@institution_login_required` (`authentication/decorators.py`) guards protected views: it re-validates a STUDENT's `student_id` against MongoDB on every request (not just trusting the cookie). **Individual views additionally check `request.session.get("role")` matches the expected role** — this per-view role check is the authorization layer; keep it when adding views.

Faculty promotion/toggle actions require a second gate: `request.session["promotion_unlocked"]`, set by a separate password prompt (`faculty_promotion_page`).

The `_ROLE_REDIRECT` dict in `views.py` maps roles to their dashboard URL names.

## Key Business Logic (all in `authentication/views.py`)

- **Required offices depend on `student_type`**: Day Scholars need LIBRARY/COLLEGE/DEPARTMENT (3); Hostellers add HOSTEL (4). This branching recurs across `student_dashboard`, `no_due_certificate`, `promote_students`, and the report/status aggregation pipelines — update all of them together.
- **Attempt/cooldown system**: students get 2 attempts per office. A 2nd rejection sets a 24h `cooldown_expiry`. `reset_expired_no_dues()` (in `utils.py`) is called at the top of most dashboards — it expires stale `PENDING` requests (older than 3 min → back to `NOT_SENT`, decrementing attempts) and clears elapsed cooldowns. Cooldown datetimes may be naive or tz-aware; code normalizes to UTC before comparing.
- **Global no-due access toggle** (`check_no_due_access_status`): faculty enable/disable the whole request process with an `auto_disable_at` timestamp; the check lazily auto-disables when that time passes. All student request views short-circuit if this is off.
- **Semester promotion** (`promote_students`): moves students up the `progression` map (sem→sem, year increments on even→odd transitions). Guarded by a `promotion_in_progress` lock in `portal_settings` (via `find_one_and_update`) to prevent concurrent runs. Blocks if target semester already populated (would merge batches) or if sem-8 students still present. Resets all no-dues on promotion.
- **7.5 scheme students** bypass hostel receipt upload.

## File Uploads

Hostel fee receipts upload to **Cloudinary** (`cloudinary.uploader.upload`), not local disk, storing `secure_url` + `public_id`. On reject/expiry the code attempts `destroy()` as both `raw` (PDF) and `image` (jpg/png) resource types since the type isn't tracked. The `media/` directory and `save_receipt()` in `utils.py` are a legacy local-storage path still referenced by `send_no_due_request` for non-hostel — prefer the Cloudinary flow.

## Reports

`office_report_pdf_view` generates styled PDFs with ReportLab (`NumberedCanvas` adds page numbers). `office_report_preview_api` / `office_student_status_api` return paginated JSON using MongoDB `$facet` aggregation. `_get_report_students_pipeline` builds the shared aggregation pipeline (student → no_due lookup → computed `office_status`) for the three report types (`year`, `branch`, `year_branch`).

## Configuration

Environment variables (loaded from `.env` via python-dotenv in `settings.py`):
- `MONGO_URI` — MongoDB connection string (required; app data is unavailable without it)
- `SECRET_KEY`, `DEBUG` (`"True"`/`"False"`)
- `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`

`DEBUG` controls cookie security flags (`SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` off in dev). Deployed on Render (`CSRF_TRUSTED_ORIGINS`, Procfile). Sessions last 2h with sliding expiry.

## Conventions

- All routing is flat in `authentication/urls.py`; views are all function-based in one large `views.py`. URL names (e.g. `student_dashboard`, `faculty_promotion`) are used for redirects throughout — keep them stable.
- Comments frequently mix English and Tamil (transliterated) and use emoji section markers. This is existing style, not a mistake.
