"""Run the durable Smart Food order-dispatch outbox worker."""

import signal
import time

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Process and retry durable Smart Food order dispatch jobs.'

    def add_arguments(self, parser):
        parser.add_argument('--interval', type=float, default=5.0)
        parser.add_argument('--batch-size', type=int, default=50)
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        from smartfood.services.dispatch_job_service import DispatchJobService
        from smartfood.services.loyalty_settlement_service import (
            reconcile_due_bot_order_loyalty,
        )

        interval = max(0.25, float(options['interval']))
        batch_size = max(1, min(int(options['batch_size']), 500))
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

        self.stdout.write(self.style.SUCCESS('Smart Food dispatch worker started'))
        auto_dispatch = getattr(settings, 'SMARTFOOD_AUTO_DISPATCH', True)
        if not auto_dispatch:
            self.stdout.write(
                'SMARTFOOD_AUTO_DISPATCH is disabled; loyalty repair remains active',
            )
        while running:
            loyalty = reconcile_due_bot_order_loyalty(limit=batch_size)
            if loyalty['reconciled']:
                self.stdout.write(
                    f"reconciled loyalty for {loyalty['reconciled']} order(s)",
                )
            if auto_dispatch:
                result = DispatchJobService.process_due(limit=batch_size)
                if result['claimed']:
                    self.stdout.write(
                        f"processed {result['claimed']} job(s); "
                        f"completed {result['completed']}",
                    )
            if run_once:
                break
            time.sleep(interval)
        self.stdout.write('Smart Food dispatch worker stopped')
