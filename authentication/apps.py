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


def _enable_sqlite_wal(sender, connection, **kwargs):
    """Enable Write-Ahead Logging (WAL) and 30s busy timeout for SQLite."""
    if connection.vendor == 'sqlite':
        try:
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA synchronous = NORMAL;")
                cursor.execute("PRAGMA busy_timeout = 30000;")
        except Exception as exc:
            logger.warning("Could not set SQLite PRAGMA journal_mode=WAL: %s", exc)


class AuthenticationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'authentication'

    def ready(self):
        from django.db.backends.signals import connection_created
        connection_created.connect(_enable_sqlite_wal)

        argv = sys.argv
        if len(argv) > 1 and argv[1] in _SKIP_COMMANDS:
            return

        # Under `runserver` autoreload the parent process should skip startup;
        # only the reloaded child (RUN_MAIN=true) runs it. Gunicorn: RUN_MAIN
        # is unset, so it proceeds normally.
        if "runserver" in argv and os.environ.get("RUN_MAIN") != "true":
            return

        try:
            from .db_indexes import ensure_indexes
            ensure_indexes()
        except Exception as exc:
            logger.error("Index initialization skipped: %s", exc)

        try:
            from .scheduler import start_scheduler
            start_scheduler()
        except Exception as exc:
            logger.error("Scheduler failed to start: %s", exc)

