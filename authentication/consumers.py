import logging
import re
from datetime import datetime, timezone
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async

logger = logging.getLogger("nodue.ws")

VALID_ROLES = {"STUDENT", "FACULTY", "LIBRARY", "HOSTEL", "COLLEGE", "DEPARTMENT"}


@database_sync_to_async
def _get_session_data(scope):
    """Safely extracts session variables in a thread-pool to avoid SynchronousOnlyOperation."""
    session = scope.get("session")
    if not session:
        logger.warning("[WS] scope has no 'session' key — SessionMiddlewareStack may be missing.")
        return {}
    try:
        # Access any key to force the lazy session to load from the DB.
        _ = session.session_key
        return {
            "role": session.get("role"),
            "student_id": session.get("student_id"),
            "department": session.get("department"),
        }
    except Exception as exc:
        logger.error("[WS] Error reading session: %s", exc, exc_info=True)
        return {}


def _clean_group_name(name):
    """Channels group names must be alphanumeric, hyphens, underscores, or periods < 100 chars."""
    clean = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", str(name))
    return clean[:95]


class PortalConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer for the No Due Clearance Portal.

    Each authenticated user is placed into one or more channel groups:
      • STUDENT  → student_<student_id>  +  students_global
      • FACULTY  → faculty_group         +  students_global
      • LIBRARY/HOSTEL/COLLEGE → office_<ROLE>  +  offices_all
      • DEPARTMENT → office_DEPARTMENT[_<dept>]  +  offices_all

    Office reject/approve views broadcast to the student's private group via
    events.notify_student(); this consumer delivers the payload to the browser.
    """

    async def connect(self):
        session_data = await _get_session_data(self.scope)
        self.role = session_data.get("role")
        self.student_id = session_data.get("student_id")
        self.department = session_data.get("department")
        self.subscribed_groups = set()

        if not self.role or self.role not in VALID_ROLES:
            logger.warning(
                "[WS] Rejecting connection — unauthenticated or unknown role=%s",
                self.role,
            )
            await self.accept()
            await self.close(code=4401)
            return

        # ── Group Subscription based on Role ──
        if self.role == "STUDENT":
            if not self.student_id:
                logger.warning("[WS] STUDENT connection rejected — no student_id in session.")
                await self.accept()
                await self.close(code=4401)
                return
            student_group = _clean_group_name(f"student_{self.student_id}")
            await self._add_group(student_group)
            await self._add_group("students_global")
            logger.info(
                "[WS] STUDENT connected: student_id=%s  private_group=%s",
                self.student_id, student_group,
            )

        elif self.role == "FACULTY":
            await self._add_group("faculty_group")
            await self._add_group("students_global")

        elif self.role in ("LIBRARY", "HOSTEL", "COLLEGE"):
            office_group = f"office_{self.role}"
            await self._add_group(office_group)
            await self._add_group("offices_all")

        elif self.role == "DEPARTMENT":
            await self._add_group("office_DEPARTMENT")
            if self.department:
                await self._add_group(f"office_DEPARTMENT_{self.department}")
            await self._add_group("offices_all")

        await self.accept()
        logger.info(
            "[WS] Connection accepted: role=%s id=%s groups=%s",
            self.role,
            self.student_id or self.department or "-",
            sorted(self.subscribed_groups),
        )

        await self.send_json({
            "type": "connection_established",
            "role": self.role,
            "connected_at": datetime.now(timezone.utc).isoformat(),
        })

    async def disconnect(self, close_code):
        for group in list(self.subscribed_groups):
            try:
                await self.channel_layer.group_discard(group, self.channel_name)
            except Exception as exc:
                logger.debug("[WS] Error discarding group %s: %s", group, exc)
        self.subscribed_groups.clear()
        logger.info(
            "[WS] Disconnected: role=%s code=%s",
            getattr(self, "role", "-"), close_code,
        )

    async def receive_json(self, content, **kwargs):
        msg_type = content.get("type")
        if msg_type == "ping":
            await self.send_json({"type": "pong", "timestamp": datetime.now(timezone.utc).isoformat()})
        elif msg_type == "heartbeat":
            await self.send_json({"type": "heartbeat_ack", "timestamp": datetime.now(timezone.utc).isoformat()})

    async def _add_group(self, group_name):
        safe = _clean_group_name(group_name)
        await self.channel_layer.group_add(safe, self.channel_name)
        self.subscribed_groups.add(safe)

    # ── Channel-layer → WebSocket delivery ──
    async def portal_event(self, event):
        """
        Called by the channel layer when a message with type='portal_event' is sent
        to any group this consumer has joined.  Forwards the payload to the browser.
        """
        payload = event.get("payload", {})
        logger.info(
            "[WS] Delivering event=%s to role=%s id=%s",
            payload.get("event"), getattr(self, "role", "?"),
            getattr(self, "student_id", None) or getattr(self, "department", "-"),
        )
        await self.send_json(payload)
