"""Run the isolated durable Telegram status/broadcast delivery worker."""

import signal
import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Process and retry Smart Food Telegram status and broadcast messages.'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=float, default=2.0)
        parser.add_argument('--batch-size', type=int, default=25)
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        from smartfood.services.outbound_message_service import OutboundMessageService

        interval = max(0.25, float(options['interval']))
        batch_size = max(1, min(int(options['batch_size']), 100))
        run_once = bool(options['once'])
        running = True

        def stop(*_args):
            nonlocal running
            running = False

        for sig in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGINT', None)):
            if sig is not None:
                try:
                    signal.signal(sig, stop)
                except (OSError, ValueError):
                    pass

        self.stdout.write(self.style.SUCCESS('Smart Food Telegram worker started'))
        while running:
            result = OutboundMessageService.process_due(limit=batch_size)
            if result['claimed']:
                self.stdout.write(
                    f"processed {result['claimed']} message(s); "
                    f"sent {result['sent']}; failed {result['failed']}; "
                    f"skipped {result['skipped']}; retrying {result['retrying']}",
                )
            if run_once:
                break
            time.sleep(interval)
        self.stdout.write('Smart Food Telegram worker stopped')
