"""Runtime customer-bot credential resolution.

The database value is an optional operator-managed override. Deployments that
already provide CUSTOMER_BOT_TOKEN keep working because the environment remains
the fallback. Callers only receive the raw value inside the server process.
"""
from django.conf import settings
from django.db import OperationalError, ProgrammingError


def customer_bot_token():
    """Return the runtime override, then the deployment environment fallback."""
    try:
        from smartfood.models import BotConfig

        override = (
            BotConfig.objects.filter(pk=1)
            .values_list('bot_token', flat=True)
            .first()
        )
    except (OperationalError, ProgrammingError):
        # Startup and migrations can call credential-aware code before the new
        # column/table exists. Falling back is safe and keeps deploys bootable.
        override = ''
    return (override or getattr(settings, 'CUSTOMER_BOT_TOKEN', '') or '').strip()


def customer_bot_token_source():
    try:
        from smartfood.models import BotConfig

        if (
            BotConfig.objects.filter(pk=1)
            .exclude(bot_token='')
            .exists()
        ):
            return 'runtime'
    except (OperationalError, ProgrammingError):
        pass
    return 'environment' if getattr(settings, 'CUSTOMER_BOT_TOKEN', '') else 'none'


def mask_bot_token(token):
    """Expose enough to identify a BotFather token without leaking it."""
    token = (token or '').strip()
    if not token:
        return ''
    bot_id, separator, secret = token.partition(':')
    if not separator:
        return f'••••{token[-4:]}' if len(token) > 4 else '••••'
    tail = secret[-4:] if len(secret) >= 4 else ''
    return f'{bot_id}:••••{tail}'
