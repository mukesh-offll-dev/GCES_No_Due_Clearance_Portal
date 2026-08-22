import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient

logger = logging.getLogger("nodue.mongo")

# ─────────────────────────────────────────────────────────────
#  Ensure .env is loaded even if mongo.py is imported directly
# ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "").strip()

# ─────────────────────────────────────────────────────────────
#  Connection pool sizing
#  Each gunicorn worker process holds its OWN MongoClient + pool.
#  With W workers and maxPoolSize=P, MongoDB sees up to W*P sockets.
#  Keep minPoolSize at >= 1 to keep warm connections alive and avoid
#  paying a 300ms TLS/DNS penalty on incoming requests.
# ─────────────────────────────────────────────────────────────
_MAX_POOL = int(os.environ.get("MONGO_MAX_POOL_SIZE", "20"))
_MIN_POOL = int(os.environ.get("MONGO_MIN_POOL_SIZE", "2"))

if not MONGO_URI:
    logger.critical(
        "CRITICAL: MONGO_URI is not set in environment or .env file! "
        "Application database (no_dues_portal) is unavailable. "
        "Please configure MONGO_URI in your .env or server environment."
    )

# ─────────────────────────────────────────────────────────────
#  Build MongoClient kwargs dynamically based on the URI
# ─────────────────────────────────────────────────────────────
mongo_kwargs = {
    "serverSelectionTimeoutMS": int(os.environ.get("MONGO_SERVER_SELECTION_MS", "5000")),
    "connectTimeoutMS": int(os.environ.get("MONGO_CONNECT_TIMEOUT_MS", "5000")),
    "socketTimeoutMS": int(os.environ.get("MONGO_SOCKET_TIMEOUT_MS", "15000")),
    "maxPoolSize": _MAX_POOL,
    "minPoolSize": _MIN_POOL,
    "maxIdleTimeMS": 120000,
    "waitQueueTimeoutMS": int(os.environ.get("MONGO_WAIT_QUEUE_MS", "5000")),
    "retryWrites": True,
    "retryReads": True,
    "heartbeatFrequencyMS": 10000,
    "appname": "gces_nodue_portal",
}

# Only configure TLS if using Atlas (mongodb+srv), or if explicitly specified in URI/env
is_srv = MONGO_URI.startswith("mongodb+srv://")
has_tls_param = "tls=true" in MONGO_URI.lower() or "ssl=true" in MONGO_URI.lower()
env_tls = os.environ.get("MONGO_TLS", "").lower() in ("true", "1", "yes")

if is_srv or has_tls_param or env_tls:
    mongo_kwargs["tls"] = True
    mongo_kwargs["tlsAllowInvalidCertificates"] = True

try:
    if MONGO_URI:
        client = MongoClient(MONGO_URI, **mongo_kwargs)
    else:
        # Fallback dummy client if MONGO_URI is missing (allows app to boot with error logs)
        client = MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)
except Exception as exc:
    logger.critical("Failed to initialize MongoClient: %s", exc)
    client = MongoClient("mongodb://127.0.0.1:27017", serverSelectionTimeoutMS=2000)

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
