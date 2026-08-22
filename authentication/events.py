"""
Centralized event broadcasting helpers for the No Due Portal.

Provides synchronous broadcasting methods to send real-time WebSocket
events through Django Channels layer to students, offices, faculty, and global groups.
"""
import logging
import re
from datetime import datetime, date, timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from bson import ObjectId

logger = logging.getLogger("nodue.events")


def _now_iso():
    """Timezone-aware UTC ISO timestamp (so browsers parse it as UTC, not local)."""
    return datetime.now(timezone.utc).isoformat()


def _sanitize_for_json(obj):
    """Recursively convert ObjectIds and datetimes to JSON-serializable types."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
        return [_sanitize_for_json(item) for item in obj]
    elif isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def _clean_group_name(name):
    """Channels group names must be alphanumeric, hyphens, underscores, or periods < 100 chars."""
    clean = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", str(name))
    return clean[:95]


def _broadcast_to_group(group_name, payload):
    """
    Broadcast a JSON-safe event to a Channels group.

    All callers run in a synchronous context — Django views (executed in the
    ASGI worker's thread pool) and the background maintenance thread.  We
    always use ``async_to_sync`` as the bridge.

    IMPORTANT: the old ``asyncio.get_running_loop() + create_task()`` path was
    removed because ``create_task`` is fire-and-forget and silently drops events
    when the task is garbage-collected before the event loop gets to run it.
    ``async_to_sync`` submits the coroutine to the parent event loop via
    ``run_coroutine_threadsafe`` and blocks until it completes — guaranteed
    delivery or a logged exception.
    """
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            logger.debug("[WS] No channel layer configured; skipping broadcast.")
            return

        clean_payload = _sanitize_for_json(payload)
        safe_group = _clean_group_name(group_name)

        msg = {
            "type": "portal_event",
            "payload": clean_payload,
        }

        async_to_sync(channel_layer.group_send)(safe_group, msg)

        logger.info(
            "[WS] Broadcasted event=%s to group=%s",
            clean_payload.get("event"), safe_group,
        )
    except Exception as exc:
        logger.warning(
            "[WS] Failed to broadcast event=%s to group=%s: %s",
            payload.get("event", "?"), group_name, exc,
            exc_info=True,
        )


# ─────────────────────────────────────────────────────────────
#  Public Event Dispatchers
# ─────────────────────────────────────────────────────────────

def notify_student(student_id, event_name, data=None):
    """
    Send an event to a specific student (e.g. request approved, rejected, timeout, cooldown expired).
    """
    if not student_id:
        return
    payload = {
        "event": event_name,
        "student_id": str(student_id),
        "timestamp": _now_iso(),
        **(data or {}),
    }
    _broadcast_to_group(f"student_{student_id}", payload)


def notify_all_students(event_name, data=None):
    """
    Send an event to all currently connected students (e.g. No Due Access locked/unlocked).
    """
    payload = {
        "event": event_name,
        "timestamp": _now_iso(),
        **(data or {}),
    }
    _broadcast_to_group("students_global", payload)


def notify_office(office_name, event_name, data=None, department=None):
    """
    Send an event to an office dashboard (e.g. new pending request, request approved/rejected, counters).
    """
    if not office_name:
        return
    office_upper = str(office_name).upper()
    payload = {
        "event": event_name,
        "office": office_upper,
        "timestamp": _now_iso(),
        **(data or {}),
    }
    
    # Broadcast to general office group (e.g. office_HOSTEL, office_LIBRARY, office_COLLEGE, office_DEPARTMENT)
    _broadcast_to_group(f"office_{office_upper}", payload)
    
    # If specific department (e.g. office_DEPARTMENT_CSE)
    if department and office_upper == "DEPARTMENT":
        dept_upper = str(department).upper()
        _broadcast_to_group(f"office_DEPARTMENT_{dept_upper}", payload)


def notify_faculty(event_name, data=None):
    """
    Send an event to all connected faculty dashboards (e.g. student promotions, 7.5 updates, stats).
    """
    payload = {
        "event": event_name,
        "timestamp": _now_iso(),
        **(data or {}),
    }
    _broadcast_to_group("faculty_group", payload)


def notify_all_offices(event_name, data=None):
    """
    Send an event to all office dashboards simultaneously (e.g. student cohort promotion).
    """
    payload = {
        "event": event_name,
        "timestamp": _now_iso(),
        **(data or {}),
    }
    _broadcast_to_group("offices_all", payload)
