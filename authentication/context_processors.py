"""
Global context processors for GCES No Due Clearance Portal.
Provides dynamic active and all departments to all templates and partials.
"""
from .departments import get_active_departments, get_all_departments


def departments_processor(request):
    """
    Supplies `global_active_departments`, `global_all_departments`,
    and safe session information to every rendered template.
    """
    session = getattr(request, "session", None)
    session_role = session.get("role") if session else None
    session_dept = session.get("department") if session else None
    session_student_id = session.get("student_id") if session else None

    try:
        active_depts = get_active_departments()
        all_depts = get_all_departments(active_only=False)
        return {
            "global_active_departments": active_depts,
            "global_all_departments": all_depts,
            "current_user_role": session_role,
            "current_user_dept": session_dept,
            "current_student_id": session_student_id,
        }
    except Exception:
        return {
            "global_active_departments": [],
            "global_all_departments": [],
            "current_user_role": session_role,
            "current_user_dept": session_dept,
            "current_student_id": session_student_id,
        }
