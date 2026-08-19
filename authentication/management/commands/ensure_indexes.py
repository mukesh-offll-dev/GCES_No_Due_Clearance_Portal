from django.core.management.base import BaseCommand

from authentication.db_indexes import ensure_indexes


class Command(BaseCommand):
    help = "Create all required MongoDB indexes (idempotent)."

    def handle(self, *args, **options):
        created = ensure_indexes()
        if created:
            self.stdout.write(self.style.SUCCESS(
                "Ensured indexes:\n  " + "\n  ".join(created)))
        else:
            self.stdout.write(self.style.SUCCESS(
                "All indexes already present (or none could be created — check logs)."))
