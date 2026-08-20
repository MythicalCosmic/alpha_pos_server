"""Customer-safe order message copy and durable Telegram queue entrypoints."""

from smartfood.models import BotConfig

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
    """Compatibility entrypoint: persist a retryable localized message."""
    return queue_order_status(getattr(bot_order, 'id', None), event)


def queue_order_status(bot_order_id, event):
    from smartfood.services.outbound_message_service import queue_order_status as queue

    return queue(bot_order_id, event)
