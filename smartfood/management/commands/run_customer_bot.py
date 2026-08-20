"""Long-poll the customer Telegram bot (getUpdates) and serve the Mini App entry.

The Mini App remains browsable even while ordering is closed, so every reply
offers the WebApp button. BotConfig.enabled only changes the accompanying copy
and the authoritative ordering gates inside the API.

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

from smartfood.credentials import customer_bot_token

logger = logging.getLogger('smartfood.bot')
_API = 'https://api.telegram.org/bot{token}/{method}'

_CLOSED = {
    'uz': 'Hozircha buyurtma qabul qilinmayapti, lekin menyuni ko‘rishingiz mumkin.',
    'ru': 'Сейчас заказы не принимаются, но вы можете посмотреть меню.',
    'en': 'Ordering is closed right now, but you can still browse the menu.',
}

_OPEN = {
    'uz': {
        'text': 'Xush kelibsiz! Menyuni ochib, buyurtma berishingiz mumkin.',
        'button': 'Menyuni ochish',
    },
    'ru': {
        'text': 'Добро пожаловать! Откройте меню, чтобы сделать заказ.',
        'button': 'Открыть меню',
    },
    'en': {
        'text': 'Welcome! Open the menu to place your order.',
        'button': 'Open menu',
    },
}


class Command(BaseCommand):
    help = 'Long-poll the customer Telegram bot (getUpdates) for the Mini App.'

    def add_arguments(self, parser):
        parser.add_argument('--poll-timeout', type=int, default=25,
                            help='Telegram long-poll timeout (seconds).')

    def handle(self, *args, **options):
        self._running = True

        def _stop(*_):
            self._running = False

        for sig in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGINT', None)):
            if sig is not None:
                try:
                    signal.signal(sig, _stop)
                except (ValueError, OSError):
                    pass  # not on the main thread

        poll_timeout = max(0, int(options.get('poll_timeout') or 25))
        self.stdout.write(self.style.SUCCESS('customer bot polling started'))
        offset = None
        active_token = None
        menu_token = None
        parked_notice = False
        while self._running:
            token = customer_bot_token()
            if not token:
                if not parked_notice:
                    self.stderr.write(
                        'Customer bot token is not configured — polling is parked.'
                    )
                    parked_notice = True
                active_token = None
                menu_token = None
                offset = None
                time.sleep(5)
                continue
            parked_notice = False
            if token != active_token:
                # A token changed in the admin console. Switch credentials
                # without restarting the container and reset the update cursor,
                # because update ids belong to the bot represented by the token.
                offset = None
                try:
                    configured = self._configure_token(token)
                except Exception:
                    configured = False
                    logger.warning('Telegram bot setup failed; retrying', exc_info=True)
                if not configured:
                    time.sleep(3)
                    continue
                active_token = token
                menu_token = None
            if token != menu_token:
                try:
                    if self._configure_menu_button(token):
                        menu_token = token
                except Exception:
                    # The inline /start button remains available even if the
                    # persistent chat-menu shortcut cannot be installed yet.
                    logger.warning(
                        'Telegram chat menu setup failed; retrying after polling',
                        exc_info=True,
                    )
            try:
                params = {'timeout': poll_timeout}
                if offset is not None:
                    params['offset'] = offset
                resp = requests.get(_API.format(token=token, method='getUpdates'),
                                    params=params, timeout=poll_timeout + 15)
                payload = self._response_payload(resp)
                if not getattr(resp, 'ok', False) or payload.get('ok') is not True:
                    error_code = payload.get('error_code')
                    logger.warning(
                        'Telegram getUpdates failed (HTTP %s, code %s): %s',
                        getattr(resp, 'status_code', 'unknown'),
                        error_code,
                        payload.get('description', 'unknown error'),
                    )
                    if error_code == 409:
                        # A webhook may have been installed after startup. Force
                        # deleteWebhook setup to run again before the next poll.
                        active_token = None
                    time.sleep(3)
                    continue
                updates = payload.get('result') or []
            except Exception:
                logger.warning('Telegram getUpdates failed; retrying', exc_info=True)
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
        from notifications.services.customer_bot import (
            build_reply,
            mark_reachable,
            send_webapp_entry,
        )
        from smartfood.models import BotConfig
        msg = update.get('message') or update.get('edited_message') or {}
        chat = msg.get('chat') or {}
        chat_id = chat.get('id')
        if not chat_id:
            return
        language = ((msg.get('from') or {}).get('language_code') or 'uz')[:2]
        language = language if language in _OPEN else 'uz'
        payload = build_reply(
            chat_id,
            language=language,
            enabled=BotConfig.load().enabled,
        )
        try:
            if send_webapp_entry(token, payload):
                mark_reachable(chat_id)
        except Exception:
            logger.debug('open menu reply failed', exc_info=True)

    @classmethod
    def _configure_token(cls, token):
        """Remove webhook mode before starting the mutually-exclusive poller."""
        response = requests.post(
            _API.format(token=token, method='deleteWebhook'),
            json={'drop_pending_updates': False},
            timeout=10,
        )
        if not cls._telegram_ok(response):
            payload = cls._response_payload(response)
            logger.warning(
                'Telegram deleteWebhook was rejected (HTTP %s, code %s): %s',
                getattr(response, 'status_code', 'unknown'),
                payload.get('error_code'),
                payload.get('description', 'unknown error'),
            )
            return False

        return True

    @classmethod
    def _configure_menu_button(cls, token):
        """Best-effort persistent Web App shortcut; inline replies remain live."""
        webapp_url = getattr(settings, 'CUSTOMER_WEBAPP_URL', '')
        if not webapp_url:
            return True
        response = requests.post(
            _API.format(token=token, method='setChatMenuButton'),
            json={
                'menu_button': {
                    'type': 'web_app',
                    'text': _OPEN['uz']['button'],
                    'web_app': {'url': webapp_url},
                },
            },
            timeout=10,
        )
        if not cls._telegram_ok(response):
            payload = cls._response_payload(response)
            logger.warning(
                'Telegram setChatMenuButton was rejected (HTTP %s, code %s): %s',
                getattr(response, 'status_code', 'unknown'),
                payload.get('error_code'),
                payload.get('description', 'unknown error'),
            )
            return False
        return True

    @staticmethod
    def _response_payload(response):
        try:
            payload = response.json() or {}
        except (TypeError, ValueError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _telegram_ok(cls, response):
        return (
            bool(getattr(response, 'ok', False))
            and cls._response_payload(response).get('ok') is True
        )
