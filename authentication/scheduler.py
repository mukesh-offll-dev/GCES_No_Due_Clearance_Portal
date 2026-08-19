"""
Lightweight in-process background scheduler.

Runs the maintenance cycle every MAINTENANCE_INTERVAL seconds in a daemon
thread. Each gunicorn worker starts its own thread, but run_maintenance_cycle()
uses a MongoDB lock so only one worker actually does the work per tick.

No external dependency (Celery/APScheduler) — this stays a single self-hosted
process fleet, which is what the deployment target calls for.
"""
import os
import time
import logging
import threading

logger = logging.getLogger("nodue.scheduler")

MAINTENANCE_INTERVAL = int(os.environ.get("MAINTENANCE_INTERVAL_SECONDS", "60"))

_started = False
_lock = threading.Lock()


def _loop():
    # Import lazily so Django apps are fully loaded before we touch Mongo.
    from .maintenance import run_maintenance_cycle
    logger.info("Maintenance scheduler started (interval=%ss)", MAINTENANCE_INTERVAL)
    while True:
        try:
            run_maintenance_cycle()
        except Exception as exc:  # a stuck/failed tick must never kill the loop
            logger.exception("Scheduler tick failed: %s", exc)
        time.sleep(MAINTENANCE_INTERVAL)


def start_scheduler():
    """Start the daemon thread once per process. Idempotent and thread-safe."""
    global _started
    if os.environ.get("DISABLE_BACKGROUND_MAINTENANCE") == "1":
        logger.info("Background maintenance disabled via env flag.")
        return
    with _lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_loop, name="nodue-maintenance", daemon=True)
    thread.start()
