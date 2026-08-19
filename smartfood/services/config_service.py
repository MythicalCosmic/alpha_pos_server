"""Load + flip the BotConfig singleton (runtime bot + delivery settings)."""
import re
from decimal import Decimal, InvalidOperation

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
_NON_NEGATIVE_DECIMALS = {
    'delivery_fee',
    'free_delivery_threshold',
    'min_order_amount',
    'loyalty_earn_per',
    'loyalty_point_value',
}


def _admin_config_dict(cfg):
    data = config_dict(cfg)
    data.update({
        'loyalty_earn_per': int(cfg.loyalty_earn_per),
        'loyalty_point_value': int(cfg.loyalty_point_value),
    })
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
        errors = {}
        token_supplied = 'bot_token' in values and values['bot_token'] is not None
        token = None
        if token_supplied:
            token = str(values['bot_token']).strip()
            if token and not _BOT_TOKEN_PATTERN.fullmatch(token):
                return ServiceResponse.validation_error({
                    'bot_token': 'Enter a valid BotFather token.',
                })
        parsed = {}
        for key in _EDITABLE:
            if key in values and values[key] is not None:
                value = values[key]
                if key in _NON_NEGATIVE_DECIMALS:
                    try:
                        value = Decimal(str(value))
                    except (InvalidOperation, TypeError, ValueError):
                        errors[key] = 'Enter a valid amount.'
                        continue
                    if not value.is_finite() or value < 0:
                        errors[key] = 'Use zero or a positive amount.'
                        continue
                elif key == 'default_tip_options':
                    if not isinstance(value, list) or len(value) > 10:
                        errors[key] = 'Provide up to 10 whole, non-negative amounts.'
                        continue
                    tips = []
                    invalid_tip = False
                    for item in value:
                        if isinstance(item, bool):
                            invalid_tip = True
                            break
                        if isinstance(item, int):
                            tip = item
                        elif isinstance(item, str) and item.isascii() and item.isdecimal():
                            tip = int(item)
                        else:
                            invalid_tip = True
                            break
                        tips.append(tip)
                    if invalid_tip:
                        errors[key] = 'Use whole, non-negative amounts.'
                        continue
                    if any(item < 0 for item in tips):
                        errors[key] = 'Use whole, non-negative amounts.'
                        continue
                    value = tips
                elif key == 'default_lang' and value not in {'uz', 'ru', 'en'}:
                    errors[key] = 'Choose Uzbek, Russian, or English.'
                    continue
                parsed[key] = value
        if errors:
            return ServiceResponse.validation_error(errors)
        if token_supplied:
            cfg.bot_token = token
        for key, value in parsed.items():
            setattr(cfg, key, value)
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
