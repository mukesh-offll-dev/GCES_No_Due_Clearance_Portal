"""
Global context processors for GCES No Due Clearance Portal.
Provides dynamic active and all departments to all templates and partials.
"""
from .departments import get_active_departments, get_all_departments

# Roles that get the desktop-first sidebar layout (presentation only).
_OFFICE_SHELL_ROLES = {"LIBRARY", "COLLEGE", "HOSTEL", "DEPARTMENT", "FACULTY"}

_ROLE_LABELS = {
    "LIBRARY": "Library Office",
    "COLLEGE": "College Office",
    "HOSTEL": "Hostel Office",
    "FACULTY": "Faculty Advisor",
    "STUDENT": "Student",
}


def _role_label(role, dept):
    if role == "DEPARTMENT":
        return f"{dept} Department" if dept else "Department"
    return _ROLE_LABELS.get(role, "")


def departments_processor(request):
    """
    Supplies `global_active_departments`, `global_all_departments`,
    and safe session information to every rendered template.
    """
    session = getattr(request, "session", None)
    session_role = session.get("role") if session else None
    session_dept = session.get("department") if session else None
    session_student_id = session.get("student_id") if session else None

    ui = {
        "current_user_role": session_role,
        "current_user_dept": session_dept,
        "current_student_id": session_student_id,
        # Layout helpers (UI only — authorization stays in the views)
        "use_office_shell": session_role in _OFFICE_SHELL_ROLES,
        "portal_role_label": _role_label(session_role, session_dept),
    }

    try:
        active_depts = get_active_departments()
        all_depts = get_all_departments(active_only=False)
        return {
            "global_active_departments": active_depts,
            "global_all_departments": all_depts,
            **ui,
        }
    except Exception:
        return {
            "global_active_departments": [],
            "global_all_departments": [],
            **ui,
        }
