"""Deliver owner-app push notifications and send the daily summary.

Runs as the ``owner_push`` compose sidecar. Each pass sends due outbox rows
through FCM (with retries and backoff) and, once per day after
``OWNER_DAILY_SUMMARY_AT`` (Tashkent time), queues yesterday's summary.
"""
import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from admins.services import fcm, owner_push

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Send owner-app push notifications and the daily summary'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.running = True
        self.summarised_day = None
        self.passes = 0

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=int, default=5, help='Seconds between passes (default: 5)')
        parser.add_argument('--once', action='store_true', help='Run one pass, then exit')

    def handle(self, *args, **options):
        interval = max(1, int(options['interval']))
        if options['once']:
            self.run_pass()
            return
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)
        self.stdout.write(self.style.SUCCESS(
            f'Owner push worker started (interval={interval}s, fcm={"on" if fcm.is_configured() else "off"})',
        ))
        while self.running:
            self.run_pass()
            for _ in range(interval):
                if not self.running:
                    break
                time.sleep(1)

    def run_pass(self):
        close_old_connections()
        try:
            day = owner_push.daily_summary_due()
            if day and day != self.summarised_day:
                if not owner_push.daily_summary_sent(day):
                    owner_push.send_daily_summary(day)
                self.summarised_day = day
            sent, failed = owner_push.deliver_batch()
            if sent or failed:
                logger.info('owner push: sent=%s failed=%s', sent, failed)
            self.passes += 1
            if self.passes % 720 == 0:
                owner_push.purge()
        except Exception:  # noqa: BLE001 — keep the worker alive; the next pass retries
            logger.exception('owner push pass failed')
        finally:
            close_old_connections()

    def _stop(self, *_):
        self.running = False
