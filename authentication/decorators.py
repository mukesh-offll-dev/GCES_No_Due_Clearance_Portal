from django.shortcuts import redirect
from .mongo import students_col
from bson import ObjectId
from bson.errors import InvalidId


def institution_login_required(view_func):
    """
    Guards every protected view.

    • For STUDENT role  → validates that the session student_id still exists
      in MongoDB (server-side check, not just a cookie value).
    • For all other roles → checks that 'role' key is present in the session.
    """
    def wrapper(request, *args, **kwargs):
        role = request.session.get("role")

        if not role:
            # No session at all → back to login
            return redirect("index")

        if role == "STUDENT":
            # Extra server-side validation: make sure the student record exists
            student_id_raw = request.session.get("student_id")
            if not student_id_raw:
                request.session.flush()
                return redirect("index")

            try:
                student = students_col.find_one(
                    {"_id": ObjectId(student_id_raw)},
                    {"_id": 1}   # projection: only fetch the ID field (fast)
                )
            except (InvalidId, Exception):
                request.session.flush()
                return redirect("index")

            if not student:
                # Student ID in session no longer exists in DB
                request.session.flush()
                return redirect("index")

        return view_func(request, *args, **kwargs)

    return wrapper
