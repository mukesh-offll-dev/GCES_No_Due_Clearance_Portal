import os
import sys
import logging

from django.apps import AppConfig

logger = logging.getLogger("nodue")

# manage.py commands that must NOT trigger index building / the scheduler.
_SKIP_COMMANDS = {
    "migrate", "makemigrations", "collectstatic", "ensure_indexes",
    "run_maintenance", "shell", "test", "createsuperuser", "dbshell",
    "check", "showmigrations", "sqlmigrate", "diffsettings",
}


class AuthenticationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'authentication'

    def ready(self):
        argv = sys.argv
        if len(argv) > 1 and argv[1] in _SKIP_COMMANDS:
            return
        # One-off `manage.py <command>` runs (rotate_credentials, load_credentials, …)
        # must not start background threads: they race the command's own imports.
        # Only a server (runserver / gunicorn / uvicorn / daphne) runs the startup work.
        if argv and os.path.basename(argv[0]).lower().startswith("manage") and \
                (len(argv) < 2 or argv[1] != "runserver"):
            return

        # Under `runserver` autoreload the parent process should skip startup;
        # only the reloaded child (RUN_MAIN=true) runs it. Gunicorn: RUN_MAIN
        # is unset, so it proceeds normally.
        if "runserver" in argv and os.environ.get("RUN_MAIN") != "true":
            return

        import threading

        # Import everything the background threads use HERE, on the main thread.
        # Importing pymongo / app modules from two threads at once can trip
        # Python's import-lock deadlock detection ("deadlock detected by
        # _ModuleLock"), which skipped index building or crashed the process.
        try:
            import pymongo.errors  # noqa: F401
            import cloudinary.uploader  # noqa: F401
            from . import mongo, db_indexes, departments, maintenance, promotion, events, accounts  # noqa: F401
        except Exception as exc:
            logger.warning("Startup pre-import failed: %s", exc)

        def _bg_ensure_indexes():
            try:
                from .db_indexes import ensure_indexes
                ensure_indexes()
            except Exception as exc:
                logger.warning("Index initialization skipped: %s", exc)
            try:
                from .departments import ensure_default_departments
                ensure_default_departments()
            except Exception as exc:
                logger.warning("Department seeding skipped: %s", exc)
            try:
                from .accounts import ensure_default_accounts
                ensure_default_accounts()          # create missing office/department logins (never overwrites)
            except Exception as exc:
                logger.warning("Default login seeding skipped: %s", exc)

        threading.Thread(target=_bg_ensure_indexes, name="nodue-index-init", daemon=True).start()

        try:
            from .scheduler import start_scheduler
            start_scheduler()
        except Exception as exc:
            logger.error("Scheduler failed to start: %s", exc)

        # Real-time safety check: an in-memory channel layer cannot deliver
        # WebSocket events across worker processes. In production that silently
        # breaks live UI updates, so make the misconfiguration impossible to miss.
        try:
            from django.conf import settings
            backend = settings.CHANNEL_LAYERS.get("default", {}).get("BACKEND", "")
            if not settings.DEBUG and "InMemory" in backend:
                logger.warning(
                    "CHANNEL_LAYERS is using the in-memory backend in a non-DEBUG "
                    "deployment. Real-time WebSocket events will NOT reach clients "
                    "connected to other worker processes. Set REDIS_URL to enable a "
                    "shared Redis channel layer."
                )
        except Exception:
            pass

