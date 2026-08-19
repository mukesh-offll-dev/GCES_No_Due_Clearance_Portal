import os
import logging
from pymongo import MongoClient

logger = logging.getLogger("nodue.mongo")

MONGO_URI = os.environ.get("MONGO_URI")

# ─────────────────────────────────────────────────────────────
#  Connection pool sizing
#  Each gunicorn worker process holds its OWN MongoClient + pool.
#  With W workers and maxPoolSize=P, MongoDB sees up to W*P sockets.
#  Keep P modest so a 25-worker fleet stays well under Mongo's limit.
#  Override per-worker with MONGO_MAX_POOL_SIZE (default 20).
# ─────────────────────────────────────────────────────────────
_MAX_POOL = int(os.environ.get("MONGO_MAX_POOL_SIZE", "20"))
_MIN_POOL = int(os.environ.get("MONGO_MIN_POOL_SIZE", "0"))

if not MONGO_URI:
    # Fail loudly at import time — the whole app is useless without Mongo,
    # and a silent None client produces confusing errors deep in views.
    logger.critical("MONGO_URI is not set — application data will be unavailable.")

client = MongoClient(
    MONGO_URI,
    # Fail fast instead of hanging the request thread on a dead DB.
    serverSelectionTimeoutMS=int(os.environ.get("MONGO_SERVER_SELECTION_MS", "5000")),
    connectTimeoutMS=int(os.environ.get("MONGO_CONNECT_TIMEOUT_MS", "5000")),
    socketTimeoutMS=int(os.environ.get("MONGO_SOCKET_TIMEOUT_MS", "20000")),
    # Connection pool — bounded per worker (see note above).
    maxPoolSize=_MAX_POOL,
    minPoolSize=_MIN_POOL,
    maxIdleTimeMS=60000,
    waitQueueTimeoutMS=int(os.environ.get("MONGO_WAIT_QUEUE_MS", "10000")),
    # Automatic, safe retries for transient network blips.
    retryWrites=True,
    retryReads=True,
    # Heartbeat so dead sockets are detected quickly.
    heartbeatFrequencyMS=10000,
    appname="gces_nodue_portal",
    tls=True,
    tlsAllowInvalidCertificates=True,  # handles lab/corporate DNS interception
)

db = client["no_dues_portal"]

# Collections
institution_logs = db["institution_logs"]
no_due_col       = db["no_due_requests"]
students_col     = db["students"]
promotion_logs   = db["promotion_logs"]
portal_settings  = db["portal_settings"]


def ping():
    """Lightweight health check — returns True if MongoDB is reachable."""
    try:
        client.admin.command("ping")
        return True
    except Exception as exc:
        logger.error("MongoDB ping failed: %s", exc)
        return False
