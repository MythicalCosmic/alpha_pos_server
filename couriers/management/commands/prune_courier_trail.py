"""Delete old courier GPS breadcrumbs (LocationTrailPoint).

The trail is also pruned opportunistically at end of shift (services.set_online),
but this command is the cron-friendly fleet-wide sweep:

    python manage.py prune_courier_trail            # default retention
    python manage.py prune_courier_trail --days 3
"""
from django.core.management.base import BaseCommand

from couriers import services


class Command(BaseCommand):
    help = 'Prune courier GPS trail breadcrumbs older than N days (default: %d).' % (
        services.TRAIL_RETENTION_DAYS)

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=services.TRAIL_RETENTION_DAYS,
                            help='Delete trail points older than this many days.')

    def handle(self, *args, **options):
        days = options['days']
        deleted = services.prune_trail(days=days)
        self.stdout.write(self.style.SUCCESS(
            f'Pruned {deleted} location trail point(s) older than {days} day(s).'))
