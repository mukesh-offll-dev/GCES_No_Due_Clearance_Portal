import functools
import logging
from django.shortcuts import redirect
from .mongo import students_col
from bson import ObjectId
from bson.errors import InvalidId

logger = logging.getLogger("nodue")

VALID_ROLES = {"STUDENT", "FACULTY", "LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"}


def _add_no_cache_headers(response):
    """Attach strict anti-caching headers to prevent back-button caching."""
    if response is not None:
        response["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
    return response


def institution_login_required(view_func):
    """
    Guards every protected view.

    • For STUDENT role  → validates that the session student_id still exists
      in MongoDB (server-side check, not just a cookie value).
    • For all other roles → checks that 'role' key is present in the session and is valid.
    • Ensures anti-cache headers are set on all protected responses.
    • Rejects invalid/expired sessions immediately with session flush and redirect.
    """
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        role = request.session.get("role")

        if not role or role not in VALID_ROLES:
            # No session or invalid role → flush and redirect to login
            request.session.flush()
            response = redirect("index")
            return _add_no_cache_headers(response)

        if role == "STUDENT":
            # Extra server-side validation: make sure the student record exists
            student_id_raw = request.session.get("student_id")
            if not student_id_raw:
                request.session.flush()
                response = redirect("index")
                return _add_no_cache_headers(response)

            try:
                student = students_col.find_one(
                    {"_id": ObjectId(student_id_raw)},
                    {"_id": 1}   # projection: only fetch the ID field (fast)
                )
            except (InvalidId, Exception) as exc:
                logger.warning("Invalid student ID in session or DB error: %s", exc)
                request.session.flush()
                response = redirect("index")
                return _add_no_cache_headers(response)

            if not student:
                # Student ID in session no longer exists in DB
                request.session.flush()
                response = redirect("index")
                return _add_no_cache_headers(response)

        response = view_func(request, *args, **kwargs)
        return _add_no_cache_headers(response)

    return wrapper

