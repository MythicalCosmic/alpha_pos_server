"""Push order updates to the customer's Telegram chat (best-effort, never raises)."""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_API = 'https://api.telegram.org/bot{token}/sendMessage'

_MESSAGES = {
    'dispatched': {
        'uz': 'Buyurtmangiz qabul qilindi! 👨‍🍳 Tez orada tayyorlanadi.',
        'ru': 'Ваш заказ принят! 👨‍🍳 Скоро начнём готовить.',
        'en': 'Your order is confirmed! 👨‍🍳 We are preparing it.',
    },
    'rejected': {
        'uz': 'Kechirasiz, buyurtmangiz qabul qilinmadi.',
        'ru': 'Извините, ваш заказ отклонён.',
        'en': 'Sorry, your order could not be accepted.',
    },
}


def notify_customer(bot_order, event):
    """Send a localized status message to the customer's Telegram chat."""
    token = getattr(settings, 'CUSTOMER_BOT_TOKEN', '') or ''
    customer = getattr(bot_order, 'customer', None)
    chat_id = getattr(customer, 'telegram_id', None)
    if not token or not chat_id:
        return False
    lang = getattr(customer, 'language', 'uz') or 'uz'
    msg = _MESSAGES.get(event, {})
    text = msg.get(lang) or msg.get('en') or ''
    if not text:
        return False
    if event == 'rejected' and bot_order.reject_reason:
        text = f'{text}\n{bot_order.reject_reason}'
    try:
        requests.post(_API.format(token=token),
                      json={'chat_id': chat_id, 'text': text}, timeout=10)
        return True
    except Exception:
        logger.debug('telegram customer notify failed', exc_info=True)
        return False
