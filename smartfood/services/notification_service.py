"""Push order updates to the customer's Telegram chat (best-effort, never raises)."""
import logging

import requests
from smartfood.credentials import customer_bot_token
from smartfood.models import BotConfig

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

_TECHNICAL_REJECTION = {
    'uz': (
        'Kechirasiz, texnik nosozlik sabab buyurtmangizni hozir qabul qila '
        'olmadik. Iltimos, qayta urinib koʻring yoki yordam xizmatiga murojaat qiling{contact}.'
    ),
    'ru': (
        'Извините, из-за технической неполадки мы не смогли сейчас '
        'принять ваш заказ. Пожалуйста, повторите позже или свяжитесь '
        'с поддержкой{contact}.'
    ),
    'en': (
        'Sorry, a technical problem prevented us from accepting your order. '
        'Please try again or contact support{contact}.'
    ),
}


def _support_contact(config):
    if config.support_phone:
        return f': {config.support_phone}'
    if config.support_telegram:
        return f': Telegram {config.support_telegram}'
    if config.support_email:
        return f': {config.support_email}'
    return ''


def technical_rejection_reason(customer=None):
    lang = getattr(customer, 'language', 'uz') or 'uz'
    template = _TECHNICAL_REJECTION.get(lang) or _TECHNICAL_REJECTION['en']
    text = template.format(contact=_support_contact(BotConfig.load()))
    return text[:200]


def notify_customer(bot_order, event):
    """Send a localized status message to the customer's Telegram chat."""
    token = customer_bot_token()
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
        response = requests.post(
            _API.format(token=token),
            json={'chat_id': chat_id, 'text': text},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            logger.warning(
                'telegram customer notify was refused (event=%s, chat=%s)',
                event,
                chat_id,
            )
            return False
        return True
    except (requests.RequestException, ValueError, TypeError):
        logger.warning(
            'telegram customer notify failed (event=%s, chat=%s)',
            event,
            chat_id,
            exc_info=True,
        )
        return False
