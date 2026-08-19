"""
Production middleware: structured request logging + a global safety net so a
single failing request can never crash a worker or leak a stack trace.
"""
import time
import uuid
import logging

from django.http import JsonResponse, HttpResponse
from django.conf import settings
from pymongo.errors import (
    PyMongoError,
    ServerSelectionTimeoutError,
    NetworkTimeout,
    AutoReconnect,
)

access_logger = logging.getLogger("nodue.access")
error_logger = logging.getLogger("nodue.error")

# Paths we don't want to spam the access log with.
_QUIET_PREFIXES = ("/static/", "/favicon.ico")

_DB_DOWN_MESSAGE = (
    "The service is temporarily unavailable. Please try again in a moment."
)


class RequestLogMiddleware:
    """Logs one line per request with a correlation id and duration."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = uuid.uuid4().hex[:12]
        start = time.monotonic()
        response = self.get_response(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        path = request.path
        if not path.startswith(_QUIET_PREFIXES):
            role = None
            try:
                role = request.session.get("role")
            except Exception:
                pass
            access_logger.info(
                "rid=%s %s %s -> %s %dms role=%s ip=%s",
                request.request_id, request.method, path,
                getattr(response, "status_code", "?"), duration_ms,
                role, _client_ip(request),
            )
        return response


class ExceptionHandlingMiddleware:
    """
    Converts unhandled exceptions into safe responses and logs them.
    DB connectivity errors -> 503 (retryable); everything else -> 500.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    # JSON endpoints (see urls.py): status + report preview APIs return JSON.
    _JSON_PATHS = ("/office/student-status", "/office/report/preview")

    def process_exception(self, request, exception):
        rid = getattr(request, "request_id", "-")
        path = request.path.rstrip("/")
        wants_json = (
            any(path.startswith(p) for p in self._JSON_PATHS)
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "")
        )

        # ── Database unavailable → 503, ask the client to retry ──
        if isinstance(exception, (ServerSelectionTimeoutError, NetworkTimeout,
                                  AutoReconnect)):
            error_logger.error("rid=%s DB unavailable on %s: %s",
                               rid, request.path, exception)
            return self._respond(wants_json, 503, _DB_DOWN_MESSAGE, rid)

        if isinstance(exception, PyMongoError):
            error_logger.exception("rid=%s DB error on %s", rid, request.path)
            return self._respond(wants_json, 503, _DB_DOWN_MESSAGE, rid)

        # ── Anything else → 500, log full traceback ──
        error_logger.exception("rid=%s Unhandled error on %s", rid, request.path)
        if settings.DEBUG:
            return None  # let Django's debug page render in development
        return self._respond(
            wants_json, 500,
            "Something went wrong. Our team has been notified.", rid,
        )

    @staticmethod
    def _respond(wants_json, status, message, rid):
        if wants_json:
            return JsonResponse({"error": message, "request_id": rid}, status=status)
        return HttpResponse(
            "%s (ref: %s)" % (message, rid),
            status=status, content_type="text/plain",
        )


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "-")
