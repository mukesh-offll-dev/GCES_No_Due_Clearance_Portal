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
