"""Long-poll the customer Telegram bot (getUpdates) and serve the Mini App entry.

Greets + offers the WebApp "open menu" button when the bot is ENABLED
(BotConfig.enabled — flippable at runtime from the operator console with NO
restart); when disabled, replies that ordering is closed and hides the button.

    python manage.py run_customer_bot

Run a SINGLE instance only — Telegram forbids getUpdates from two pollers, and a
webhook must not be set at the same time (this command deletes the webhook on
startup). The HTTP `web` service keeps serving the REST API + websockets; this
process only drives the Telegram chat.
"""
import logging
import signal
import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger('smartfood.bot')
_API = 'https://api.telegram.org/bot{token}/{method}'

_CLOSED = {
    'uz': 'Kechirasiz, hozircha buyurtma qabul qilinmayapti. 😔',
    'ru': 'Извините, приём заказов сейчас закрыт. 😔',
    'en': 'Sorry, we are not accepting orders right now. 😔',
}


class Command(BaseCommand):
    help = 'Long-poll the customer Telegram bot (getUpdates) for the Mini App.'

    def add_arguments(self, parser):
        parser.add_argument('--poll-timeout', type=int, default=25,
                            help='Telegram long-poll timeout (seconds).')

    def handle(self, *args, **options):
        token = getattr(settings, 'CUSTOMER_BOT_TOKEN', '') or ''
        if not token:
            self.stderr.write('CUSTOMER_BOT_TOKEN is not set — nothing to poll.')
            return

        self._running = True

        def _stop(*_):
            self._running = False

        for sig in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGINT', None)):
            if sig is not None:
                try:
                    signal.signal(sig, _stop)
                except (ValueError, OSError):
                    pass  # not on the main thread

        # Drop any webhook so getUpdates is permitted.
        try:
            requests.post(_API.format(token=token, method='deleteWebhook'),
                          json={'drop_pending_updates': False}, timeout=10)
        except Exception:
            logger.debug('deleteWebhook failed', exc_info=True)

        poll_timeout = max(0, int(options.get('poll_timeout') or 25))
        self.stdout.write(self.style.SUCCESS('customer bot polling started'))
        offset = None
        while self._running:
            try:
                params = {'timeout': poll_timeout}
                if offset is not None:
                    params['offset'] = offset
                resp = requests.get(_API.format(token=token, method='getUpdates'),
                                    params=params, timeout=poll_timeout + 15)
                updates = (resp.json() or {}).get('result', []) if resp.ok else []
            except Exception:
                logger.debug('getUpdates failed', exc_info=True)
                time.sleep(3)
                continue
            for upd in updates:
                offset = upd.get('update_id', 0) + 1
                try:
                    self._handle(token, upd)
                except Exception:
                    logger.exception('update handling failed')
        self.stdout.write('customer bot polling stopped')

    def _handle(self, token, update):
        from smartfood.models import BotConfig
        msg = update.get('message') or update.get('edited_message') or {}
        chat = msg.get('chat') or {}
        chat_id = chat.get('id')
        if not chat_id:
            return
        if BotConfig.load().enabled:
            # Reuse the canonical greeting + WebApp "open menu" button.
            from notifications.services import customer_bot
            customer_bot.handle_update(update)
        else:
            lang = ((msg.get('from') or {}).get('language_code') or 'uz')[:2]
            text = _CLOSED.get(lang, _CLOSED['en'])
            try:
                requests.post(_API.format(token=token, method='sendMessage'),
                              json={'chat_id': chat_id, 'text': text}, timeout=10)
            except Exception:
                logger.debug('closed reply failed', exc_info=True)
