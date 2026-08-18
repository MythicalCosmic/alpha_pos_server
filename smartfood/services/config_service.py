"""Load + flip the BotConfig singleton (runtime bot + delivery settings)."""
import re

from django.conf import settings

from smartfood.credentials import (
    customer_bot_token,
    customer_bot_token_source,
    mask_bot_token,
)
from smartfood.models import BotConfig
from smartfood.serializers import config_dict
from base.helpers.response import ServiceResponse

# Fields an operator may set via POST /api/admins/smartfood/config.
_EDITABLE = (
    'enabled', 'currency', 'delivery_fee', 'free_delivery_threshold',
    'min_order_amount', 'default_tip_options', 'service_area', 'default_lang',
    'loyalty_earn_per', 'loyalty_point_value',
    'support_phone', 'support_telegram', 'support_email', 'support_chat_id',
)

_BOT_TOKEN_PATTERN = re.compile(r'^\d{5,20}:[A-Za-z0-9_-]{20,}$')


def _admin_config_dict(cfg):
    data = config_dict(cfg)
    token = customer_bot_token()
    data['bot'] = {
        'token_configured': bool(token),
        'token_masked': mask_bot_token(token),
        'token_source': customer_bot_token_source(),
        'environment_fallback_configured': bool(
            getattr(settings, 'CUSTOMER_BOT_TOKEN', '')
        ),
    }
    return data


class BotConfigService:
    @staticmethod
    def get():
        return ServiceResponse.success(data=config_dict(BotConfig.load()))

    @staticmethod
    def get_admin():
        return ServiceResponse.success(data=_admin_config_dict(BotConfig.load()))

    @staticmethod
    def update(values):
        cfg = BotConfig.load()
        if 'bot_token' in values and values['bot_token'] is not None:
            token = str(values['bot_token']).strip()
            if token and not _BOT_TOKEN_PATTERN.fullmatch(token):
                return ServiceResponse.validation_error({
                    'bot_token': 'Enter a valid BotFather token.',
                })
            cfg.bot_token = token
        for key in _EDITABLE:
            if key in values and values[key] is not None:
                setattr(cfg, key, values[key])
        cfg.save()
        return ServiceResponse.success(
            data=_admin_config_dict(BotConfig.load()),
            message='Config updated',
        )

    @staticmethod
    def set_enabled(flag):
        cfg = BotConfig.load()
        cfg.enabled = bool(flag)
        cfg.save()
        return ServiceResponse.success(data={'enabled': cfg.enabled})
