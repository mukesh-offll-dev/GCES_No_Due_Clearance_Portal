import time

from django.core.management.base import BaseCommand

from authentication.maintenance import run_maintenance_cycle


class Command(BaseCommand):
    help = (
        "Run the No Due maintenance cycle (expire stale PENDING requests, clear "
        "cooldowns, delete orphaned receipts). Run once for cron/systemd timers, "
        "or with --loop for a standalone daemon."
    )

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true",
                            help="Run continuously instead of a single cycle.")
        parser.add_argument("--interval", type=int, default=60,
                            help="Seconds between cycles when --loop is set.")

    def handle(self, *args, **options):
        if options["loop"]:
            self.stdout.write("Running maintenance loop (Ctrl-C to stop)...")
            while True:
                self._run_once()
                time.sleep(options["interval"])
        else:
            self._run_once()

    def _run_once(self):
        result = run_maintenance_cycle()
        if result is None:
            self.stdout.write("Skipped (another worker holds the lock).")
        else:
            self.stdout.write(self.style.SUCCESS("Maintenance cycle: %s" % result))
